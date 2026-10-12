import sys
import asyncio
import json
import logging
from pathlib import Path
from jupyter_client import KernelManager as JupyterKM
from jupyter_client.kernelspec import KernelSpec

logger = logging.getLogger(__name__)

EXECUTION_TIMEOUT = 120  # segundos

class KernelEnvironmentError(Exception):
    """Excepción descriptiva cuando el proyecto del usuario carece de entorno virtual o ipykernel."""
    pass


class KernelManager:
    """Gestiona el ciclo de vida del kernel IPython."""

    def __init__(self):
        self.project_dir: Path | None = None
        self.venv_python: Path | None = None
        self.cwd: Path | None = None
        self.last_execution_count: int | None = None
        self.last_startup_error: str | None = None
        self._km: JupyterKM | None = None
        self._kc = None  # KernelClient

    async def start(self, project_dir: Path, cwd: Path | None = None):
        """Inicia el kernel exclusivamente con el Python del .venv del proyecto de datos.
        Nunca recurre al intérprete del servidor para preservar el aislamiento estricto.
        """
        if self._km is not None and self.is_alive:
            return  # Ya fue iniciado y está activo

        if self._km is not None:
            # Si el kernel anterior murió, limpiar recursos antes de recrearlo
            await self.shutdown()

        self.project_dir = project_dir.resolve()
        self.cwd = cwd.resolve() if cwd is not None else (self.project_dir / "notebooks").resolve()
        self.cwd.mkdir(parents=True, exist_ok=True)
        
        python_candidates = [
            self.project_dir / ".venv" / "bin" / "python",
            self.project_dir / ".venv" / "Scripts" / "python.exe",
            self.project_dir / "venv" / "bin" / "python",
            self.project_dir / "venv" / "Scripts" / "python.exe",
        ]
        found_python = next((p for p in python_candidates if p.exists()), None)
        
        if not found_python:
            err_msg = (
                f"No se encontró un entorno virtual (.venv) en el proyecto ({self.project_dir}). "
                "El servidor data-shield-mcp no contiene ni comparte un kernel global propio para no contaminar tus proyectos. "
                "Para solucionar esto, ejecuta en la terminal de la raíz de tu proyecto: `uv add ipykernel` "
                "(o crea un entorno virtual con `virtualenv` / `python -m venv .venv` e instala ipykernel)."
            )
            self.last_startup_error = err_msg
            logger.warning(err_msg)
            raise KernelEnvironmentError(err_msg)

        self.venv_python = found_python
        self._km = JupyterKM()
        self._km._kernel_spec = KernelSpec(
            display_name='Python 3 (.venv)',
            language='python',
            argv=[
                str(self.venv_python),
                '-m', 'ipykernel_launcher',
                '-f', '{connection_file}'
            ]
        )

        try:
            # Iniciar en hilo separado (operación bloqueante)
            await asyncio.to_thread(
                self._km.start_kernel,
                cwd=str(self.cwd)
            )

            self._kc = self._km.client()
            self._kc.start_channels()
            # Esperar a que el kernel esté listo
            await asyncio.to_thread(
                self._kc.wait_for_ready,
                timeout=30
            )
            self.last_startup_error = None
            logger.info("Kernel IPython iniciado exitosamente con %s en %s", self.venv_python, self.cwd)
        except Exception as e:
            await self.shutdown()
            err_msg = (
                f"El entorno virtual del proyecto ({self.venv_python}) no pudo iniciar el kernel IPython: {e}. "
                "Asegúrate de que 'ipykernel' esté instalado dentro de ese entorno virtual. "
                f"Puedes instalarlo ejecutando en la raíz de '{self.project_dir}': `uv add ipykernel`."
            )
            self.last_startup_error = err_msg
            logger.error(err_msg)
            raise KernelEnvironmentError(err_msg) from e

    async def set_cwd(self, cwd: Path):
        """Cambia el directorio de trabajo del kernel en ejecución sin ensuciar historial ni execution_count."""
        self.cwd = cwd.resolve()
        self.cwd.mkdir(parents=True, exist_ok=True)
        if self._kc and self.is_alive:
            try:
                code = f"import os; os.chdir({repr(str(self.cwd))})"
                await self.execute(code, store_history=False, silent=True)
                logger.info("Directorio de trabajo del kernel cambiado a: %s", self.cwd)
            except Exception as e:
                logger.warning("No se pudo cambiar el CWD en el kernel: %s", e)

    async def get_widget_state(self) -> dict | None:
        """Obtiene el estado serializado de ipywidgets si está presente en memoria."""
        if not self._kc or not self.is_alive:
            return None
        code = (
            "import json as __json\n"
            "try:\n"
            "    import ipywidgets as __widgets\n"
            "    __state = __widgets.Widget.get_manager_state()\n"
            "    if __state and __state.get('state'):\n"
            "        print('__MCP_WIDGET_STATE__' + __json.dumps(__state) + '__MCP_WIDGET_STATE__')\n"
            "except Exception:\n"
            "    pass\n"
        )
        try:
            raw = await self.execute(code, store_history=False)
            full_stdout = "".join(
                out.get("text", "")
                for out in raw
                if out.get("output_type") == "stream" and out.get("name") == "stdout"
            )
            if "__MCP_WIDGET_STATE__" in full_stdout:
                json_str = full_stdout.split("__MCP_WIDGET_STATE__")[1].split("__MCP_WIDGET_STATE__")[0]
                return json.loads(json_str)
        except Exception as e:
            logger.debug("Error al obtener estado de widgets: %s", e)
        return None

    async def execute(
        self,
        code: str,
        timeout: int = EXECUTION_TIMEOUT,
        store_history: bool = True,
        silent: bool = False
    ) -> list[dict]:
        """Ejecuta código y retorna los outputs crudos (formato nbformat)."""
        if not self._kc or not self.is_alive:
            msg = self.last_startup_error or (
                "El kernel de Jupyter no está en ejecución. "
                "Asegúrate de que el proyecto tenga un entorno virtual (.venv) con ipykernel instalado ('uv add ipykernel')."
            )
            raise KernelEnvironmentError(msg)

        msg_id = self._kc.execute(code, store_history=store_history, silent=silent)
        outputs = []

        # Recopilar mensajes del canal iopub
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"[TimeoutError: La celda tardó más de {timeout}s. "
                    "Optimiza el código de Pandas o toma una muestra]"
                )

            try:
                msg = await asyncio.to_thread(
                    self._kc.get_iopub_msg,
                    timeout=min(remaining, 5.0)
                )
            except Exception:
                if not self.is_alive:
                    raise RuntimeError("El proceso del kernel terminó inesperadamente.")
                continue

            if msg['parent_header'].get('msg_id') != msg_id:
                continue

            msg_type = msg['header']['msg_type']
            content = msg['content']

            if msg_type == 'status' and content['execution_state'] == 'idle':
                break
            elif msg_type == 'execute_input':
                if store_history and 'execution_count' in content:
                    self.last_execution_count = content.get('execution_count')
            elif msg_type == 'stream':
                outputs.append({
                    'output_type': 'stream',
                    'name': content.get('name', 'stdout'),
                    'text': content.get('text', '')
                })
            elif msg_type in ('execute_result', 'display_data'):
                if store_history and content.get('execution_count') is not None:
                    self.last_execution_count = content.get('execution_count')
                outputs.append({
                    'output_type': msg_type,
                    'data': content.get('data', {}),
                    'metadata': content.get('metadata', {}),
                    'execution_count': content.get('execution_count')
                })
            elif msg_type == 'error':
                outputs.append({
                    'output_type': 'error',
                    'ename': content.get('ename', ''),
                    'evalue': content.get('evalue', ''),
                    'traceback': content.get('traceback', [])
                })

        return outputs

    async def interrupt(self):
        """Interrumpe el código en ejecución, manteniendo el estado de la memoria."""
        if self._km:
            await asyncio.to_thread(self._km.interrupt_kernel)
            logger.info("Kernel interrumpido.")

    async def restart(self):
        """Reinicia el kernel, preservando la configuración y el directorio de trabajo."""
        if self._km and self.is_alive:
            if self._kc:
                self._kc.stop_channels()
            await asyncio.to_thread(self._km.restart_kernel, now=True)
            self._kc = self._km.client()
            self._kc.start_channels()
            await asyncio.to_thread(self._kc.wait_for_ready, timeout=30)
            self.last_execution_count = None
            if self.cwd:
                code = f"import os; os.chdir({repr(str(self.cwd))})"
                await self.execute(code, store_history=False, silent=True)
            logger.info("Kernel reiniciado exitosamente.")
        elif self.project_dir:
            await self.shutdown()
            await self.start(self.project_dir, self.cwd)

    async def shutdown(self):
        """Apaga el kernel limpiamente."""
        if self._kc:
            self._kc.stop_channels()
            self._kc = None
        if self._km:
            try:
                await asyncio.to_thread(self._km.shutdown_kernel, now=True)
            except Exception as e:
                logger.debug("Error durante shutdown_kernel: %s", e)
            logger.info("Kernel apagado correctamente.")
        self._km = None
        self.last_execution_count = None

    @property
    def is_alive(self) -> bool:
        return self._km is not None and self._km.is_alive()

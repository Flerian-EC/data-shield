import os
import sys
import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import nbformat
from dotenv import load_dotenv

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from .kernel import KernelManager
from .sanitizer import sanitize_outputs

# Configurar logging hacia stderr para no corromper JSON-RPC
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("data-shield-mcp")

@dataclass
class AppState:
    """Estado compartido por todas las herramientas durante la sesión."""
    kernel: KernelManager
    workspace_root: Path | None = None
    notebooks: dict[str, nbformat.NotebookNode] = field(default_factory=dict)
    active_notebook: str | None = None


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppState]:
    # Inicializar el KernelManager sin ruta, se arrancará de forma perezosa (lazy)
    kernel = KernelManager()
    
    state = AppState(
        kernel=kernel,
        workspace_root=None
    )

    try:
        yield state
    finally:
        await kernel.shutdown()


def _parse_path_from_uri_or_str(uri_val: Any) -> Path | None:
    """Parsea de forma segura una URI o ruta string hacia un Path absoluto válido."""
    if not uri_val:
        return None
    try:
        if hasattr(uri_val, "path") and uri_val.path:
            path_str = unquote(str(uri_val.path))
        else:
            uri_str = str(uri_val).strip()
            if not uri_str or uri_str.lower() == "none":
                return None
            if uri_str.startswith("file://"):
                parsed = urlparse(uri_str)
                path_str = unquote(parsed.path)
            else:
                path_str = unquote(uri_str)

        if not path_str or path_str.strip() == "" or path_str.strip().lower() == "none":
            return None

        # Normalizar letra de unidad en Windows (/C:/path -> C:/path) en cualquier rama
        if os.name == "nt" and path_str.startswith("/") and len(path_str) > 2 and path_str[2] == ":":
            path_str = path_str[1:]

        resolved = Path(path_str).resolve()
        return resolved
    except Exception as e:
        logger.debug(f"Error parseando ruta desde {uri_val}: {e}")
        return None


def _extract_raw_from_val(val: Any) -> str | None:
    """Extrae de manera segura una ruta o URI desde diversos formatos de parámetros del cliente."""
    if not val:
        return None
    if isinstance(val, (str, Path)):
        s = str(val).strip()
        return s if s and s.lower() != "none" else None
    if isinstance(val, (list, tuple)):
        for item in val:
            extracted = _extract_raw_from_val(item)
            if extracted:
                return extracted
        return None
    if isinstance(val, dict):
        for key in ("uri", "path", "name", "rootUri", "root_uri"):
            v = val.get(key)
            if v and isinstance(v, (str, Path)):
                s = str(v).strip()
                if s and s.lower() != "none":
                    return s
        return None
    if hasattr(val, "uri"):
        u = getattr(val, "uri")
        if u and isinstance(u, (str, Path)):
            s = str(u).strip()
            return s if s and s.lower() != "none" else None
        elif u and hasattr(u, "path"):
            return str(u)
    return None


def _get_cell_source(cell: nbformat.NotebookNode) -> str:
    """Extrae el código o texto de una celda de forma normalizada como string plano."""
    src = cell.get("source", "")
    if isinstance(src, list):
        return "".join(src)
    return str(src) if src is not None else ""


def _is_plugin_or_system_dir(p: Path) -> bool:
    """Determina si un Path apunta al propio directorio del plugin o a ubicaciones del sistema."""
    try:
        resolved = p.resolve()
        resolved_str = str(resolved).lower()
        if any(marker in resolved_str for marker in (
            ".gemini/config/plugins",
            ".gemini/antigravity-cli/plugins",
            ".agents/plugins/data-shield",
            "plugins/data-shield",
            "/site-packages/",
        )):
            return True
        if (resolved / "plugin.json").exists() and (resolved / "data_shield_mcp").exists():
            return True
        if resolved.name == "data-shield-plugin" or (resolved.name == "data-shield" and (resolved / "plugin").exists()):
            return True
    except Exception:
        pass
    return False


def _score_workspace_candidate(p: Path) -> int:
    """Calcula una puntuación heurística para validar si el candidato es el espacio de trabajo del usuario."""
    score = 0
    if not p.exists() or not p.is_dir():
        return -100

    if _is_plugin_or_system_dir(p):
        score -= 50

    # Fuertes indicadores de un proyecto de datos activo del usuario
    if (p / ".venv").exists() or (p / "venv").exists():
        score += 35
    if (p / "notebooks").exists():
        score += 15
    if (p / "data").exists():
        score += 10
    if (p / "contexto_datos.md").exists():
        score += 10
    if (p / ".antigravityignore").exists() or (p / ".gitignore").exists():
        score += 5
    if (p / "pyproject.toml").exists() and not (p / "data_shield_mcp").exists():
        score += 5

    return score


async def _gather_workspace_candidates(ctx: Context) -> list[tuple[Path, int]]:
    """Recolecta posibles rutas del espacio de trabajo desde el protocolo MCP, env vars y procesos."""
    candidates: list[tuple[Path, int]] = []

    # 1. MCP Standard: Solicitar roots al cliente vía ListRootsRequest
    session = getattr(ctx, "session", None)
    if session and hasattr(session, "send_request") and callable(session.send_request):
        try:
            import mcp_types as types
            roots_result = await session.send_request(types.ListRootsRequest(), types.ListRootsResult)
            if roots_result and getattr(roots_result, "roots", None):
                for r in roots_result.roots:
                    raw_uri = getattr(r, "uri", None)
                    parsed = _parse_path_from_uri_or_str(raw_uri)
                    if parsed and parsed.exists():
                        candidates.append((parsed, 45))
        except Exception as e:
            logger.debug("ListRootsRequest falló o no está soportado por el cliente: %s", e)

    # 2. Parámetros de inicialización del cliente (client_params)
    if session:
        cp = getattr(session, "client_params", None)
        if cp:
            raw_vals = []
            for attr in (
                "root_uri", "rootUri", "root_path", "rootPath",
                "workspace_folders", "workspaceFolders",
                "trusted_workspaces", "trustedWorkspaces"
            ):
                if hasattr(cp, attr):
                    raw_vals.append(getattr(cp, attr))
            if hasattr(cp, "model_extra") and cp.model_extra:
                for key in (
                    "rootUri", "root_uri", "rootPath", "root_path",
                    "workspaceFolders", "workspace_folders",
                    "trustedWorkspaces", "trustedWorkspaces"
                ):
                    if key in cp.model_extra:
                        raw_vals.append(cp.model_extra.get(key))
            for item in raw_vals:
                raw_str = _extract_raw_from_val(item)
                parsed = _parse_path_from_uri_or_str(raw_str)
                if parsed and parsed.exists():
                    candidates.append((parsed, 40))

    # 3. Variables de entorno (conservadas por la terminal y CLI)
    env_vars = [
        ("ANTIGRAVITY_WORKSPACE", 35),
        ("WORKSPACE_ROOT", 35),
        ("WORKSPACE_FOLDER", 35),
        ("INIT_CWD", 30),
        ("PWD", 30),
        ("OLDPWD", 10),
    ]
    for var_name, prio in env_vars:
        val = os.environ.get(var_name)
        if val:
            parsed = _parse_path_from_uri_or_str(val)
            if parsed and parsed.exists():
                candidates.append((parsed, prio))

    # 4. En Linux: CWD de procesos ancestros inspeccionando /proc
    try:
        pid = os.getpid()
        for _ in range(5):
            stat_path = Path(f"/proc/{pid}/stat")
            if not stat_path.exists():
                break
            ppid = int(stat_path.read_text().split()[3])
            if ppid <= 1:
                break
            cwd_link = Path(f"/proc/{ppid}/cwd")
            if cwd_link.exists():
                resolved = cwd_link.resolve()
                if resolved.exists():
                    candidates.append((resolved, 25))
            pid = ppid
    except Exception:
        pass

    # 5. CWD del proceso actual
    candidates.append((Path(os.getcwd()).resolve(), 5))

    return candidates


async def ensure_workspace(ctx: Context, state: AppState):
    """Obtiene el workspace_root dinámicamente y levanta el kernel en ese CWD si no se ha hecho."""
    if state.workspace_root is None:
        try:
            candidates = await _gather_workspace_candidates(ctx)
            scored: list[tuple[int, Path]] = []
            seen: set[str] = set()

            for path, prio in candidates:
                p = path.resolve()
                if p.is_file():
                    p = p.parent
                p_str = str(p)
                if p_str in seen:
                    continue
                seen.add(p_str)

                total_score = prio + _score_workspace_candidate(p)
                scored.append((total_score, p))

            # Ordenar de mayor a menor puntuación heurística
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored:
                state.workspace_root = scored[0][1]
                logger.info(f"Workspace root detectado: {state.workspace_root} (score: {scored[0][0]})")
            else:
                state.workspace_root = Path(os.getcwd()).resolve()
        except Exception as e:
            logger.error(f"Fallo en detección dinámica de workspace: {e}", exc_info=True)
            state.workspace_root = Path(os.getcwd()).resolve()

        if state.workspace_root.is_file():
            state.workspace_root = state.workspace_root.parent

    if not state.kernel.is_alive or (state.kernel.project_dir and state.kernel.project_dir != state.workspace_root):
        notebooks_dir = (state.workspace_root / "notebooks").resolve()
        notebooks_dir.mkdir(parents=True, exist_ok=True)
        try:
            await state.kernel.start(state.workspace_root, cwd=notebooks_dir)
        except Exception as e:
            logger.warning(f"No se pudo inicializar el kernel durante ensure_workspace: {e}")



mcp = MCPServer("data-shield-mcp", lifespan=app_lifespan)

# Helper para errores
class UserToolError(Exception):
    pass


def _get_active_nb(state: AppState) -> nbformat.NotebookNode:
    """Obtiene el notebook activo o lanza error."""
    if not state.active_notebook:
        raise UserToolError("No hay notebook activo. Usa create_notebook primero.")
    return state.notebooks[state.active_notebook]


async def _sync_widget_state_if_needed(state: AppState):
    """Sincroniza el estado de los widgets en los metadatos del notebook si hay widgets en memoria o los purga si no quedan."""
    if not state.active_notebook or state.active_notebook not in state.notebooks:
        return
    nb = state.notebooks[state.active_notebook]
    has_widget_output = any(
        out.get("output_type") in ("execute_result", "display_data")
        and "application/vnd.jupyter.widget-view+json" in out.get("data", {})
        for cell in nb.cells if cell.cell_type == "code"
        for out in cell.get("outputs", [])
    )
    if has_widget_output:
        widget_state = await state.kernel.get_widget_state()
        if widget_state and widget_state.get("state"):
            if "widgets" not in nb.metadata:
                nb.metadata["widgets"] = {}
            nb.metadata["widgets"]["application/vnd.jupyter.widget-state+json"] = widget_state
    else:
        nb.metadata.pop("widgets", None)


def _save_notebook(state: AppState):
    """Guarda el notebook activo al disco si existe."""
    if not state.active_notebook or state.active_notebook not in state.notebooks or not state.workspace_root:
        return
    nb = state.notebooks[state.active_notebook]
    nb_path = state.workspace_root / state.active_notebook
    
    # Asegurar que el subdirectorio exista antes de guardar
    nb_path.parent.mkdir(parents=True, exist_ok=True)
    
    nbformat.write(nb, str(nb_path))


def _convert_to_nbformat_outputs(raw_outputs: list[dict]) -> list:
    """Convierte outputs crudos del kernel al formato nbformat."""
    nb_outputs = []
    for out in raw_outputs:
        output_type = out["output_type"]
        if output_type == "stream":
            nb_outputs.append(nbformat.v4.new_output(
                output_type="stream",
                name=out.get("name", "stdout"),
                text=out.get("text", "")
            ))
        elif output_type == "execute_result":
            nb_outputs.append(nbformat.v4.new_output(
                output_type=output_type,
                data=out.get("data", {}),
                metadata=out.get("metadata", {}),
                execution_count=out.get("execution_count")
            ))
        elif output_type == "display_data":
            nb_outputs.append(nbformat.v4.new_output(
                output_type=output_type,
                data=out.get("data", {}),
                metadata=out.get("metadata", {})
            ))
        elif output_type == "error":
            nb_outputs.append(nbformat.v4.new_output(
                output_type="error",
                ename=out.get("ename", ""),
                evalue=out.get("evalue", ""),
                traceback=out.get("traceback", [])
            ))
    return nb_outputs


# ──────────────────────────────────────────────
# TOOL 1: Crear Notebook
# ──────────────────────────────────────────────
@mcp.tool()
async def create_notebook(
    ctx: Context,
    filename: str = "analisis.ipynb",
    notebook_name: str | None = None,
    name: str | None = None
) -> str:
    """Crea un notebook Jupyter vacío anclado al directorio notebooks/. Si ya existe, lo carga y lo define como activo.

    Args:
        filename: Nombre del archivo .ipynb (default: analisis.ipynb, alias: notebook_name, name)
        notebook_name: Alias opcional para filename
        name: Alias opcional para filename
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    
    # Sanitizar y forzar el directorio
    raw_fname = filename if filename != "analisis.ipynb" else (notebook_name or name or filename)
    clean_filename = (raw_fname or "").strip()
    if not clean_filename or clean_filename == ".ipynb":
        clean_filename = "analisis.ipynb"
        
    if os.path.isabs(clean_filename):
        p = Path(clean_filename).resolve()
        if p.parent.name == "notebooks":
            state.workspace_root = p.parent.parent
        else:
            state.workspace_root = p.parent
        safe_filename = p.name
    else:
        safe_filename = os.path.basename(clean_filename)

    if not safe_filename.endswith(".ipynb"):
        safe_filename += ".ipynb"
    if not safe_filename or safe_filename == ".ipynb":
        safe_filename = "analisis.ipynb"
        
    safe_relative_path = f"notebooks/{safe_filename}"
    nb_path = state.workspace_root / safe_relative_path

    if nb_path.exists():
        nb = nbformat.read(str(nb_path), as_version=4)
        state.notebooks[safe_relative_path] = nb
        state.active_notebook = safe_relative_path
        await state.kernel.set_cwd(nb_path.parent)
        res = f"Notebook '{safe_relative_path}' cargado ({len(nb.cells)} celdas existentes)."
    else:
        nb = nbformat.v4.new_notebook()
        nb.metadata.update({
            "kernelspec": {
                "display_name": "Python 3 (.venv)",
                "language": "python",
                "name": "python3"
            }
        })
        
        # Asegurar que el subdirectorio (ej. notebooks/) exista antes de guardar
        nb_path.parent.mkdir(parents=True, exist_ok=True)
        
        nbformat.write(nb, str(nb_path))
        state.notebooks[safe_relative_path] = nb
        state.active_notebook = safe_relative_path
        await state.kernel.set_cwd(nb_path.parent)
        res = f"Notebook '{safe_relative_path}' creado y marcado como activo exitosamente."

    if not state.kernel.is_alive:
        env_warn = state.kernel.last_startup_error or (
            "No se detectó un kernel activo en el proyecto. Asegúrate de tener instalado 'ipykernel' en el .venv ('uv add ipykernel')."
        )
        res += f"\n\n⚠️ ADVERTENCIA DE ENTORNO:\n{env_warn}"
    return res


# ──────────────────────────────────────────────
# TOOL 2: Añadir y ejecutar celda
# ──────────────────────────────────────────────
@mcp.tool()
async def add_and_execute_cell(
    code: str | None = None,
    source: str | None = None,
    content: str | None = None,
    ctx: Context = None
) -> str:
    """Añade una nueva celda de código al final del notebook y la ejecuta.
    Retorna la salida saneada (imágenes y HTML pesado son filtrados).

    Args:
        code: Código Python a ejecutar (alias: source, content)
        source: Alias opcional para code
        content: Alias opcional para code
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)

    actual_code = code if code is not None else (source if source is not None else content)
    if actual_code is None:
        return "Error: Se requiere el código a ejecutar (parámetro 'code', 'source' o 'content')."

    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"

    cell = nbformat.v4.new_code_cell(source=actual_code)
    nb.cells.append(cell)
    cell_index = len(nb.cells) - 1

    # Ejecutar en el kernel
    try:
        raw_outputs = await state.kernel.execute(actual_code)
    except TimeoutError as e:
        cell.outputs = []
        cell.execution_count = None
        _save_notebook(state)
        return f"Error de Timeout en Celda {cell_index}: {str(e)}"
    except Exception as e:
        cell.outputs = []
        cell.execution_count = None
        _save_notebook(state)
        return f"Error inesperado al ejecutar celda: {str(e)}"

    # Guardar outputs y execution_count COMPLETOS en el notebook (para humanos)
    cell.execution_count = state.kernel.last_execution_count
    cell.outputs = _convert_to_nbformat_outputs(raw_outputs)
    await _sync_widget_state_if_needed(state)
    _save_notebook(state)

    # Retornar outputs SANITIZADOS al LLM
    sanitized = sanitize_outputs(raw_outputs)
    return f"[Celda {cell_index}] Ejecutada.\n{sanitized}"


# ──────────────────────────────────────────────
# TOOL 3: Editar y ejecutar celda
# ──────────────────────────────────────────────
@mcp.tool()
async def edit_and_execute_cell(
    cell_index: int | None = None,
    new_code: str | None = None,
    index: int | None = None,
    code: str | None = None,
    source: str | None = None,
    content: str | None = None,
    ctx: Context = None
) -> str:
    """Sobrescribe el código de una celda existente y la re-ejecuta.

    Args:
        cell_index: Índice de la celda (0-based, alias: index)
        new_code: Nuevo código Python (alias: code, source, content)
        index: Alias opcional para cell_index
        code: Alias opcional para new_code
        source: Alias opcional para new_code
        content: Alias opcional para new_code
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    
    idx = cell_index if cell_index is not None else index
    if idx is None:
        return "Error: Se requiere el índice de la celda (parámetro 'cell_index' o 'index')."

    actual_code = new_code if new_code is not None else (code if code is not None else (source if source is not None else content))
    if actual_code is None:
        return "Error: Se requiere el nuevo código Python (parámetro 'new_code', 'code' o 'source')."

    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"

    if idx < 0 or idx >= len(nb.cells):
        return f"Error: Índice {idx} fuera de rango (0-{len(nb.cells)-1})."

    cell = nb.cells[idx]
    cell.source = actual_code

    try:
        raw_outputs = await state.kernel.execute(actual_code)
    except TimeoutError as e:
        cell.outputs = []
        cell.execution_count = None
        _save_notebook(state)
        return f"Error de Timeout en Celda {idx}: {str(e)}"
    except Exception as e:
        cell.outputs = []
        cell.execution_count = None
        _save_notebook(state)
        return f"Error al ejecutar celda {idx}: {str(e)}"

    cell.execution_count = state.kernel.last_execution_count
    cell.outputs = _convert_to_nbformat_outputs(raw_outputs)
    await _sync_widget_state_if_needed(state)
    _save_notebook(state)

    sanitized = sanitize_outputs(raw_outputs)
    return f"[Celda {idx}] Editada y ejecutada.\n{sanitized}"


# ──────────────────────────────────────────────
# TOOL: Añadir celda Markdown
# ──────────────────────────────────────────────
@mcp.tool()
async def add_markdown_cell(
    markdown_text: str | None = None,
    text: str | None = None,
    content: str | None = None,
    source: str | None = None,
    ctx: Context = None
) -> str:
    """Añade una nueva celda de tipo Markdown al final del notebook.

    Args:
        markdown_text: El contenido en formato Markdown (alias: text, content, source).
        text: Alias opcional para markdown_text.
        content: Alias opcional para markdown_text.
        source: Alias opcional para markdown_text.
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)

    actual_text = markdown_text if markdown_text is not None else (text if text is not None else (content if content is not None else source))
    if actual_text is None:
        return "Error: Se requiere el contenido Markdown (parámetro 'markdown_text', 'text' o 'content')."

    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"

    cell = nbformat.v4.new_markdown_cell(source=actual_text)
    nb.cells.append(cell)
    cell_index = len(nb.cells) - 1

    _save_notebook(state)
    return f"[Celda {cell_index}] añadida (Markdown)."


# ──────────────────────────────────────────────
# TOOL: Editar celda Markdown
# ──────────────────────────────────────────────
@mcp.tool()
async def edit_markdown_cell(
    cell_index: int | None = None,
    new_markdown_text: str | None = None,
    index: int | None = None,
    markdown_text: str | None = None,
    text: str | None = None,
    content: str | None = None,
    source: str | None = None,
    ctx: Context = None
) -> str:
    """Sobrescribe el texto de una celda Markdown existente (o convierte una de código a Markdown).

    Args:
        cell_index: Índice de la celda (0-based, alias: index)
        new_markdown_text: Nuevo texto Markdown (alias: markdown_text, text, content, source)
        index: Alias opcional para cell_index
        markdown_text: Alias opcional para new_markdown_text
        text: Alias opcional para new_markdown_text
        content: Alias opcional para new_markdown_text
        source: Alias opcional para new_markdown_text
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    
    idx = cell_index if cell_index is not None else index
    if idx is None:
        return "Error: Se requiere el índice de la celda (parámetro 'cell_index' o 'index')."

    actual_text = new_markdown_text if new_markdown_text is not None else (
        markdown_text if markdown_text is not None else (
            text if text is not None else (
                content if content is not None else source
            )
        )
    )
    if actual_text is None:
        return "Error: Se requiere el nuevo texto Markdown (parámetro 'new_markdown_text', 'markdown_text' o 'text')."

    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"

    if idx < 0 or idx >= len(nb.cells):
        return f"Error: Índice {idx} fuera de rango (0-{len(nb.cells)-1})."

    cell = nb.cells[idx]
    cell.cell_type = "markdown"
    cell.source = actual_text
    
    # Limpiar atributos específicos de celdas de código si los hay
    cell.pop('outputs', None)
    cell.pop('execution_count', None)

    await _sync_widget_state_if_needed(state)
    _save_notebook(state)
    return f"[Celda {idx}] editada y configurada como Markdown."


# ──────────────────────────────────────────────
# TOOL 4: Re-ejecutar celda existente
# ──────────────────────────────────────────────
@mcp.tool()
async def execute_cell(
    cell_index: int | None = None,
    index: int | None = None,
    ctx: Context = None
) -> str:
    """Re-ejecuta una celda existente sin modificar su código.

    Args:
        cell_index: Índice de la celda (0-based, alias: index)
        index: Alias opcional para cell_index
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    
    idx = cell_index if cell_index is not None else index
    if idx is None:
        return "Error: Se requiere el índice de la celda (parámetro 'cell_index' o 'index')."

    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"

    if idx < 0 or idx >= len(nb.cells):
        return f"Error: Índice {idx} fuera de rango."

    cell = nb.cells[idx]
    cell_source = _get_cell_source(cell)

    try:
        raw_outputs = await state.kernel.execute(cell_source)
    except TimeoutError as e:
        return f"Error de Timeout en Celda {idx}: {str(e)}"
    except Exception as e:
        return f"Error al re-ejecutar celda {idx}: {str(e)}"

    cell.execution_count = state.kernel.last_execution_count
    cell.outputs = _convert_to_nbformat_outputs(raw_outputs)
    await _sync_widget_state_if_needed(state)
    _save_notebook(state)

    sanitized = sanitize_outputs(raw_outputs)
    return f"[Celda {cell_index}] Re-ejecutada.\n{sanitized}"


# ──────────────────────────────────────────────
# TOOL 5: Leer estado del notebook
# ──────────────────────────────────────────────
@mcp.tool()
async def read_notebook_status(ctx: Context) -> str:
    """Devuelve un resumen del notebook: número de celdas, código fuente
    y resumen de salidas (saneadas) de cada celda."""
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"

    parts = [f"📓 Notebook: {state.active_notebook} ({len(nb.cells)} celdas)\n"]

    for i, cell in enumerate(nb.cells):
        source = _get_cell_source(cell)
        if cell.cell_type == "markdown":
            parts.append(f"━━━ Celda {i} [Markdown] ━━━")
            if len(source) > 500:
                source = source[:500] + "\n...[markdown truncado]"
            parts.append(source)
            parts.append("")
            continue

        parts.append(f"━━━ Celda {i} [Código] ━━━")
        # Código fuente (truncado si muy largo)
        if len(source) > 500:
            source = source[:500] + "\n...[código truncado]"
        parts.append(f"```python\n{source}\n```")
        
        # Outputs saneados
        if cell.outputs:
            sanitized = sanitize_outputs(cell.outputs)
            parts.append(f"Output:\n{sanitized}")
        else:
            parts.append("Output: (sin salida)")
        parts.append("")

    return "\n".join(parts)


# ──────────────────────────────────────────────
# TOOL 6: Reiniciar kernel
# ──────────────────────────────────────────────
@mcp.tool()
async def restart_kernel(ctx: Context) -> str:
    """Reinicia el kernel IPython. Esto limpia todas las variables y estado
    en memoria. Las celdas previas necesitarán re-ejecutarse si se necesitan
    sus resultados."""
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    try:
        await state.kernel.restart()
        if not state.kernel.is_alive:
            err = state.kernel.last_startup_error or "El kernel no pudo reiniciarse."
            return f"Error al reiniciar kernel: {err}"
        return (
            "⚠️ Kernel reiniciado. Todas las variables en memoria se perdieron. "
            "Si necesitas el estado previo, re-ejecuta las celdas necesarias "
            "con execute_cell."
        )
    except Exception as e:
        return f"Error al reiniciar kernel: {str(e)}"


# ──────────────────────────────────────────────
# TOOL 7: Limpiar todos los outputs
# ──────────────────────────────────────────────
@mcp.tool()
async def clear_all_outputs(ctx: Context) -> str:
    """Limpia los outputs de todas las celdas del notebook. No afecta el código
    ni el estado del kernel."""
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"

    for cell in nb.cells:
        if cell.cell_type == "code":
            cell.outputs = []
            cell.execution_count = None

    nb.metadata.pop("widgets", None)
    _save_notebook(state)
    return f"Outputs de {len(nb.cells)} celdas limpiados en {state.active_notebook}."


# ──────────────────────────────────────────────
# TOOL 8: Eliminar celda
# ──────────────────────────────────────────────
@mcp.tool()
async def delete_cell(
    cell_index: int | None = None,
    index: int | None = None,
    ctx: Context = None
) -> str:
    """Elimina una celda específica del notebook.

    Args:
        cell_index: Índice de la celda a eliminar (0-based, alias: index)
        index: Alias opcional para cell_index
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    
    idx = cell_index if cell_index is not None else index
    if idx is None:
        return "Error: Se requiere el índice de la celda (parámetro 'cell_index' o 'index')."

    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"

    if idx < 0 or idx >= len(nb.cells):
        return f"Error: Índice {idx} fuera de rango."

    cell_source = _get_cell_source(nb.cells[idx])
    removed_source = cell_source[:100]
    del nb.cells[idx]
    await _sync_widget_state_if_needed(state)
    _save_notebook(state)

    return f"Celda {idx} eliminada (contenía: '{removed_source}...'). Nuevo total: {len(nb.cells)} celdas."

# ──────────────────────────────────────────────
# TOOL: insert_cell
# ──────────────────────────────────────────────
@mcp.tool()
async def insert_cell(
    cell_index: int | None = None,
    cell_type: str = "code",
    source: str | None = None,
    index: int | None = None,
    type: str | None = None,
    code: str | None = None,
    content: str | None = None,
    markdown_text: str | None = None,
    text: str | None = None,
    execute: bool = False,
    ctx: Context = None
) -> str:
    """Inserta una celda (código o markdown) en una posición específica.
    
    Args:
        cell_index: Índice donde insertar (0-based, alias: index, default: 0)
        cell_type: "code" o "markdown" (alias: type, default: "code")
        source: Código o texto a insertar (alias: code, content, markdown_text, text)
        index: Alias opcional para cell_index
        type: Alias opcional para cell_type
        code: Alias opcional para source
        content: Alias opcional para source
        markdown_text: Alias opcional para source
        text: Alias opcional para source
        execute: (Opcional) Si es True y cell_type="code", ejecuta la celda tras insertarla
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"
        
    actual_type = (type or cell_type or "code").strip().lower()
    if actual_type not in ("code", "markdown"):
        return f"Error: cell_type debe ser 'code' o 'markdown' (recibido: '{actual_type}')."

    actual_source = source if source is not None else (
        code if code is not None else (
            content if content is not None else (
                markdown_text if markdown_text is not None else (
                    text if text is not None else ""
                )
            )
        )
    )

    idx = cell_index if cell_index is not None else (index if index is not None else len(nb.cells))
    idx = max(0, min(idx, len(nb.cells)))
    
    if actual_type == "code":
        new_cell = nbformat.v4.new_code_cell(actual_source)
    else:
        new_cell = nbformat.v4.new_markdown_cell(actual_source)
        
    try:
        nb.cells.insert(idx, new_cell)
        
        msg = f"Celda insertada en el índice {idx}."
        
        if execute and actual_type == "code":
            try:
                raw_outputs = await state.kernel.execute(actual_source)
                new_cell.execution_count = state.kernel.last_execution_count
                new_cell.outputs = _convert_to_nbformat_outputs(raw_outputs)
                await _sync_widget_state_if_needed(state)
                sanitized = sanitize_outputs(raw_outputs)
                msg += f"\nEjecutada.\nOutput:\n{sanitized}"
            except TimeoutError as e:
                msg += f"\nError de Timeout: {str(e)}"
            except Exception as e:
                msg += f"\nError al ejecutar celda: {str(e)}"
                
        _save_notebook(state)
        return msg
    except Exception as e:
        return f"Error insertando celda: {str(e)}"

# ──────────────────────────────────────────────
# TOOL: read_cell
# ──────────────────────────────────────────────
@mcp.tool()
async def read_cell(
    cell_index: int | None = None,
    index: int | None = None,
    ctx: Context = None
) -> str:
    """Lee el código fuente y las salidas saneadas de una sola celda.
    
    Args:
        cell_index: Índice de la celda a consultar (0-based, alias: index).
        index: Alias opcional para cell_index
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"
        
    idx = cell_index if cell_index is not None else index
    if idx is None:
        return "Error: Se requiere el índice de la celda (parámetro 'cell_index' o 'index')."

    if idx < 0 or idx >= len(nb.cells):
        return f"Error: Índice {idx} fuera de rango."
        
    cell = nb.cells[idx]
    cell_source = _get_cell_source(cell)
    
    if cell.cell_type == "markdown":
        return f"━━━ Celda {idx} [Markdown] ━━━\n{cell_source}"
        
    out_msg = f"━━━ Celda {idx} [Código] ━━━\n```python\n{cell_source}\n```\n"
    if cell.outputs:
        sanitized = sanitize_outputs(cell.outputs)
        out_msg += f"Output:\n{sanitized}"
    else:
        out_msg += "Output: (sin salida)"
    return out_msg

# ──────────────────────────────────────────────
# TOOL: inspect_memory
# ──────────────────────────────────────────────
@mcp.tool()
async def inspect_memory(ctx: Context) -> str:
    """Obtiene una radiografía de las variables en memoria del kernel 
    (tipo, tamaño, shape) de forma invisible, sin añadir celdas al notebook.
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    
    code = (
        "import sys as __sys\n"
        "__mem_report = []\n"
        "for __k, __v in list(globals().items()):\n"
        "    if not __k.startswith('_') and __k not in ('In', 'Out', 'get_ipython', 'exit', 'quit', 'open'):\n"
        "        try:\n"
        "            __size = __sys.getsizeof(__v)\n"
        "            __type = type(__v).__name__\n"
        "            __shape = getattr(__v, 'shape', None)\n"
        "            __shape_str = f', shape: {__shape}' if __shape is not None else ''\n"
        "            __mem_report.append(f'- {__k}: {__type} ({__size} bytes{__shape_str})')\n"
        "        except Exception:\n"
        "            pass\n"
        "print('Variables en memoria:\\n' + '\\n'.join(__mem_report) if __mem_report else 'No hay variables de usuario definidas.')\n"
    )
    try:
        raw_outputs = await state.kernel.execute(code, store_history=False)
        return sanitize_outputs(raw_outputs)
    except Exception as e:
        return f"Error al inspeccionar memoria: {str(e)}"

# ──────────────────────────────────────────────
# TOOL: interrupt_kernel
# ──────────────────────────────────────────────
@mcp.tool()
async def interrupt_kernel(ctx: Context) -> str:
    """Detiene la ejecución colgada en el kernel (ej. bucle infinito) 
    sin perder las variables en memoria.
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    
    if not state.kernel.is_alive:
        err = state.kernel.last_startup_error or "El kernel no está en ejecución."
        return f"Error: {err}"

    await state.kernel.interrupt()
    return "El kernel ha sido interrumpido."

# ──────────────────────────────────────────────
# TOOL: run_scratchpad_code
# ──────────────────────────────────────────────
@mcp.tool()
async def run_scratchpad_code(
    code: str | None = None,
    source: str | None = None,
    content: str | None = None,
    ctx: Context = None
) -> str:
    """Ejecuta código en borrador (scratchpad).
    Es completamente invisible: no se guarda en el archivo .ipynb.
    
    Args:
        code: Código a explorar/ejecutar (alias: source, content).
        source: Alias opcional para code.
        content: Alias opcional para code.
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    
    actual_code = code if code is not None else (source if source is not None else content)
    if actual_code is None:
        return "Error: Se requiere el código a ejecutar (parámetro 'code', 'source' o 'content')."

    try:
        raw_outputs = await state.kernel.execute(actual_code, store_history=False)
        return sanitize_outputs(raw_outputs)
    except TimeoutError as e:
        return f"Error de Timeout: {str(e)}"
    except Exception as e:
        return f"Error al ejecutar borrador: {str(e)}"

# ──────────────────────────────────────────────
# TOOL: move_cell
# ──────────────────────────────────────────────
@mcp.tool()
async def move_cell(
    source_index: int | None = None,
    target_index: int | None = None,
    from_index: int | None = None,
    to_index: int | None = None,
    src: int | None = None,
    dst: int | None = None,
    ctx: Context = None
) -> str:
    """Reorganiza la narrativa moviendo una celda a una nueva posición.
    
    Args:
        source_index: Posición original (0-based, alias: from_index, src)
        target_index: Nueva posición destino (0-based, alias: to_index, dst)
        from_index: Alias opcional para source_index
        to_index: Alias opcional para target_index
        src: Alias opcional para source_index
        dst: Alias opcional para target_index
    """
    state: AppState = ctx.request_context.lifespan_context
    await ensure_workspace(ctx, state)
    try:
        nb = _get_active_nb(state)
    except UserToolError as e:
        return f"Error: {str(e)}"
        
    src_idx = source_index if source_index is not None else (from_index if from_index is not None else src)
    dst_idx = target_index if target_index is not None else (to_index if to_index is not None else dst)
    if src_idx is None or dst_idx is None:
        return "Error: Se requieren 'source_index' y 'target_index' (o 'from_index' y 'to_index')."

    num_cells = len(nb.cells)
    if src_idx < 0 or src_idx >= num_cells:
        return f"Error: source_index {src_idx} fuera de rango."
        
    dst_idx = max(0, min(dst_idx, num_cells - 1))
    
    cell = nb.cells.pop(src_idx)
    nb.cells.insert(dst_idx, cell)
    
    _save_notebook(state)
    return f"Celda movida del índice {src_idx} al {dst_idx}."

# ──────────────────────────────────────────────
# RESOURCE: info_mcp
# ──────────────────────────────────────────────
@mcp.resource("info://mcp")
def info_mcp() -> str:
    """Devuelve un texto explicativo sobre la utilidad de este servidor MCP."""
    return (
        "El servidor data-shield-mcp es una herramienta que conecta a los LLMs con un kernel "
        "de Jupyter Notebook en ejecución local. Su propósito principal es permitir la "
        "ejecución persistente de código estadístico y análisis de datos en Python, al tiempo "
        "que 'escuda' la ventana de contexto del LLM. Esto lo logra truncando DataFrames masivos "
        "y eliminando cadenas Base64 de las gráficas antes de devolver las respuestas al LLM, "
        "mientras guarda el notebook físico (.ipynb) de forma íntegra para la revisión visual del humano."
    )

# ──────────────────────────────────────────────
# CONFIGURACIÓN PYDANTIC: Permitir argumentos extra para tolerancia a fallos
# ──────────────────────────────────────────────
from pydantic import create_model, ConfigDict
for tool in mcp._tool_manager._tools.values():
    original_model = tool.fn_metadata.arg_model
    fields = {k: (v.annotation, v) for k, v in original_model.model_fields.items()}
    new_model = create_model(
        original_model.__name__,
        __base__=original_model.__bases__[0],
        __config__=ConfigDict(extra='ignore'),
        **fields
    )
    tool.fn_metadata.arg_model = new_model

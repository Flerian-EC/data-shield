# Blueprint Definitivo: data-shield-mcp (Guía de Construcción)

Este documento es la especificación técnica y de implementación **exhaustiva** para construir el servidor MCP (Model Context Protocol) en Python llamado **data-shield-mcp** utilizando el SDK v2 de MCP. 
Cualquier desarrollador o agente de IA debe ser capaz de reproducir el servidor exacto leyendo este documento.

El objetivo del servidor es ejecutar código en un Jupyter Notebook local, conectándose al `.venv` del proyecto de análisis de datos, y actuando como un **escudo** que sanitiza salidas masivas (imágenes, tablas) antes de enviarlas al LLM, pero guardando el resultado completo en el archivo `.ipynb` para revisión humana.

---

## 1. Estructura del Proyecto

El servidor debe configurarse como un paquete de Python empaquetable con `hatchling`. La estructura exacta de archivos debe ser:

```text
data_mcp/
├── pyproject.toml
└── data-shield-mcp/
    ├── __init__.py
    ├── __main__.py
    ├── kernel.py
    ├── sanitizer.py
    └── server.py
```

### Configuración de `pyproject.toml`
Debe definir el proyecto, las dependencias (`mcp[cli]>=2.1.1`, `jupyter-client>=8.10.0`, `nbformat>=5.11.1`, `python-dotenv`) y, críticamente, configurar `hatchling` para exponer un binario y reconocer el paquete:

```toml
[project]
name = "data-shield-mcp"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["jupyter-client>=8.10.0", "mcp[cli]>=2.1.1", "nbformat>=5.11.1", "python-dotenv>=1.2.3"]

[project.scripts]
dataShield-mcp = "data-shield-mcp.server:mcp.run"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["data-shield-mcp"]
```
*(Nota: `ipykernel` NO debe estar en las dependencias de producción, es responsabilidad del entorno de datos del usuario).*

---

## 2. Entrypoint y Soporte de Entornos Efímeros (`__main__.py`)

Para permitir que el servidor sea ejecutado tanto nativamente como mediante comandos efímeros de `uv` (ej. `uv run --with "mcp[cli]" mcp run data-shield-mcp/__main__.py`), el archivo `__main__.py` **debe inyectar dinámicamente** las rutas del proyecto en `sys.path`.

**Implementación Obligatoria:**
1. Obtener la raíz del proyecto a partir de `Path(__file__)` e insertarla en `sys.path[0]`.
2. Buscar la carpeta `.venv/lib/python*/site-packages` dentro de esa raíz usando `glob`, e inyectar también ese directorio en `sys.path[0]`.
3. Solo después de estos pasos, ejecutar `from data-shield-mcp.server import mcp` y llamar a `mcp.run()`.

---

## 3. Gestión del Kernel (`kernel.py`)

Esta es la pieza más compleja a nivel de concurrencia. Debe envolver `jupyter_client.KernelManager`.

**Requisitos Arquitectónicos:**
*   **Inicialización Diferida (Lazy):** `KernelManager` se instancia vacío en el `lifespan`. Recién arranca al ejecutarse la primera herramienta cuando el servidor MCP intercepta dinámicamente el `workspace_root` del cliente.
*   **Comando del Kernel:** Se debe sobrescribir explícitamente el comando nativo de Jupyter para forzar la delegación al `.venv` del proyecto cliente. En versiones modernas esto requiere modificar `spec.argv` directo en memoria:
    ```python
    from jupyter_client.kernelspec import KernelSpec

    self._km = JupyterKM()
    self._km._kernel_spec = KernelSpec(
        display_name='Python 3 (.venv)',
        language='python',
        argv=[str(venv_python), '-m', 'ipykernel_launcher', '-f', '{connection_file}']
    )
    ```
*   **Asincronía Real:** Como `jupyter_client` bloquea el hilo principal, todas las llamadas (`start_kernel`, `wait_for_ready`, `get_iopub_msg`) deben ejecutarse dentro de `await asyncio.to_thread(...)`.
*   **Ejecución (`execute`)**:
    1. Enviar el código con `msg_id = kc.execute(code)`.
    2. Entrar en un bucle `while True` iterando sobre `kc.get_iopub_msg`.
    3. **Timeouts:** Llevar control manual con `deadline = loop.time() + timeout`. Si expira, lanzar un `TimeoutError`.
    4. **Filtro de Mensajes:** Solo procesar mensajes donde `msg['parent_header'].get('msg_id') == msg_id`.
    5. **Finalización:** Romper el bucle cuando llega un mensaje de `msg_type == 'status'` y `execution_state == 'idle'`.
    6. **Recolección:** Traducir los mensajes de la terminal a formato dict para nbformat.

---

## 4. Escudo de Tokens (`sanitizer.py`)

Debe recibir la lista de outputs brutos que generó `kernel.py` y retornar un único string en texto plano seguro para el LLM.

**Reglas de Filtro:**
1.  **Imágenes y Gráficos Interactivos (`image/png`, `image/jpeg`, `ipympl` / `ipywidgets`):** Si existen estas claves en `data` (incluyendo `application/vnd.jupyter.widget-view+json`, imágenes base64 en HTML o `Canvas`), ignorar el resto y retornar literalmente `"[Gráfico renderizado localmente]"`.
2.  **DataFrames (`text/html`):** Si existe `text/html`, tomar la versión de `text/plain`. Separarla por líneas. Si excede ~13 líneas, quedarse solo con las primeras 5 y últimas 5, insertando `"... [X filas omitidas] ..."` en el medio.
3.  **Streams (`stdout/stderr`):** Limitar estrictamente a 1500 caracteres totales. Si se pasa, concatenar `"...[truncado]"` al final.
4.  **Errores (`error` / traceback):** 
    *   Usar Expresiones Regulares (`re.sub(r'\x1b\[[0-9;]*m', '', line)`) para limpiar los códigos ANSI de color obligatoriamente.
    *   Limpiar las rutas absolutas del sistema operativo en el traceback usando regex para proteger la privacidad y ahorrar tokens (ej. cambiar `/home/usuario/...` por `~/...`).

---

## 5. Primitivas MCP y Estado (`server.py`)

El núcleo expone 10 Herramientas, 1 Recurso y 1 Prompt utilizando el SDK `mcp.server.MCPServer`. Todo error atrapado en las herramientas debe devolverse como un string (ej. `return f"Error: {e}"`) y no colgar el servidor.

**Gestión de Estado (Lifespan):**
*   Utilizar `@asynccontextmanager`. Inicializar `KernelManager()` vacío y ceder (yield) un `@dataclass AppState` con `workspace_root=None`.
*   Al cerrar la sesión (finally), ejecutar `kernel.shutdown()`.

**Captura Dinámica de Entorno (`ensure_workspace`):**
*   Toda herramienta MCP invoca `await ensure_workspace(ctx, state)` en su primera línea.
*   Esta función inyectable lee `ctx.session.list_roots()` (MCP v2) o hace fallback a variables de entorno / `os.getcwd()`.
*   Una vez que obtiene el `workspace_root` absoluto del cliente (donde corre Antigravity), arranca el `KernelManager` asíncronamente con su directorio de trabajo (CWD) situado en `notebooks/` (idéntico a la ejecución nativa en VS Code), haciendo que las rutas relativas se resuelvan directamente sin necesidad de resolución dinámica en el código del notebook.

**Validación Estricta de Parámetros:**
*   Se aplica un parche (Monkey-Patch) a `mcp._tool_manager._tools` al final de `server.py` reconfigurando Pydantic con `ConfigDict(extra='forbid')`. Esto asegura que si un LLM envía un parámetro inválido o alucinado (ej. `notebook_name`), el servidor aborte y le retorne el Traceback para forzar su autocorrección, en vez de ignorarlo silenciosamente.

**Herramientas (Las 16 indispensables):**
Todas extraen el estado usando: `state = ctx.request_context.lifespan_context`.
1.  `create_notebook(filename="analisis.ipynb")`: Sanitiza `filename` aislando solo su nombre base (previniendo path traversal), lo ancla obligatoriamente a la ruta `notebooks/{safe_filename}` y lo carga/crea. Todas las demás herramientas dependen de su ruta segura interna.
2.  `add_and_execute_cell(code)`: Añade celda al final, ejecuta, guarda salidas COMPLETAS (`cell.outputs = raw_outputs`) en archivo vía `nbformat.write`, y retorna el texto pasado por `sanitizer.py`.
3.  `edit_and_execute_cell(cell_index, new_code)`: Reemplaza fuente en índice dado, borra viejos outputs, reejecuta, guarda y retorna output saneado.
4.  `execute_cell(cell_index)`: Reejecuta la celda del índice dado sin alterar su código.
5.  `add_markdown_cell(markdown_text)`: Crea una `nbformat.v4.new_markdown_cell`, la anexa y guarda el archivo.
6.  `edit_markdown_cell(cell_index, new_markdown_text)`: Modifica el índice. Fuerza `cell_type="markdown"`. **Debe purgar explícitamente** claves `outputs` y `execution_count` si las hubiera (por si antes era código).
7.  `read_notebook_status()`: Itera las celdas. 
    *   Si es código: Imprime `━━━ Celda {i} [Código] ━━━`, el código fuente (truncado a 500 max) y su output saneado.
    *   Si es markdown: Imprime `━━━ Celda {i} [Markdown] ━━━`, el contenido markdown (truncado a 500 max). **Omite silenciosamente** la sección de output.
8.  `restart_kernel()`: Llama a `kernel.restart()` advirtiendo a la IA que se perdieron variables en memoria.
9.  `clear_all_outputs()`: Recorre el notebook y fija `cell.outputs = []` y `cell.execution_count = None` en celdas de código. Guarda disco.
10. `delete_cell(cell_index)`: Elimina la celda de la lista y guarda el archivo.
11. `insert_cell(cell_index, cell_type, source, execute)`: Inserta una celda en una posición específica y opcionalmente la ejecuta si es de código.
12. `read_cell(cell_index)`: Lee el código fuente y las salidas saneadas de una sola celda, sin tener que consultar todo el notebook.
13. `inspect_memory()`: Ejecuta silenciosamente código con `sys.getsizeof()` para listar las variables, tipos y tamaños actuales en la memoria del kernel sin ensuciar el notebook físico.
14. `interrupt_kernel()`: Llama a `kernel.interrupt()` para abortar un proceso largo o colgado sin perder las variables previamente calculadas.
15. `run_scratchpad_code(code)`: Ejecuta código en borrador y retorna el resultado saneado al LLM. No guarda absolutamente nada en el archivo `.ipynb`.
16. `move_cell(source_index, target_index)`: Extrae (pop) y reinserta (insert) una celda para reorganizar la narrativa del análisis.

**Recurso de Información:**
*   `@mcp.resource("info://mcp")`: Devuelve un texto plano estático explicando qué hace este servidor MCP (su utilidad para ejecutar código localmente y escudar visualmente las salidas gráficas para proteger la ventana de contexto del LLM).
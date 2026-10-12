---
name: data-shield-skill
description: >-
  Instrucciones y procedimientos técnicos para crear, manipular, ejecutar e inspeccionar notebooks de Jupyter (.ipynb) a través del servidor data-shield-mcp. Enseña el uso de cada herramienta del MCP, la gestión del kernel y la protección de la ventana de contexto mediante sanitización de datos y gráficos. Usar cuando se trabaje con análisis de datos en Jupyter o se requiera interactuar con notebooks sin saturar el contexto.
---

# Habilidad: data-shield-skill

Esta habilidad proporciona el protocolo operativo y técnico para interactuar con notebooks de Jupyter (`.ipynb`) mediante el servidor **`data-shield-mcp`**, garantizando la ejecución en un entorno local aislado y la protección estricta de la ventana de contexto del modelo.

---

## 1. Principios de Aislamiento y Protección de Contexto

1. **Ubicación Forzosa de Notebooks:**
   - Todos los notebooks (`*.ipynb`) deben residir obligatoriamente en el subdirectorio `notebooks/` de la raíz del proyecto.
   - Dicho directorio debe estar excluido en `.antigravityignore` y `.gitignore` para evitar que el LLM intente indexar o leer el JSON crudo del notebook.
   - Si la carpeta `notebooks/` no existe en el proyecto cliente, créala con el comando de sistema `mkdir notebooks` antes de inicializar el notebook con el MCP.

2. **Prohibición de Manipulación Directa de Archivos:**
   - **NUNCA** leas, crees ni edites archivos `.ipynb` mediante herramientas de lectura de texto (`view_file`), editores de archivo, comandos de shell (`cat`, `echo`, `sed`) o scripts de Python independientes.
   - Toda interacción con el contenido y la ejecución del notebook debe realizarse **exclusivamente a través de las herramientas del servidor `data-shield-mcp`**.

3. **Directorio de Ejecución y Rutas Relativas:**
   - El kernel de Jupyter ejecuta el código situándose directamente en el directorio del notebook (`notebooks/`), emulando el comportamiento interactivo de editores como VS Code.
   - Para cargar datos o interactuar con archivos del proyecto, utiliza siempre rutas relativas referenciadas desde `notebooks/` (por ejemplo, `pd.read_csv('../data/archivo.csv')` o `Path('../data/archivo.csv')`).
   - Mantén el código reproducible y limpio; evita código condicional dinámico para resolver rutas (`BASE_DIR = ... if ...`).

4. **Visualización Segura de DataFrames:**
   - **NUNCA** uses la función `print()` para mostrar DataFrames completos (ej. `print(df)`), ya que esto vuelca texto plano masivo no estructurado.
   - Deja siempre la variable sola en la última línea de la celda de código (ej. simplemente `df`) para que Jupyter devuelva el objeto de manera nativa. El sanitizador de `data-shield-mcp` interceptará la salida, truncando tablas a 5 filas iniciales y 5 finales con un resumen intermedio, salvaguardando los tokens.
   - Para inspecciones preliminares o conteos, usa métodos selectivos como `df.head()`, `df.info()`, `df.shape` o `df.describe()`.

5. **Gráficos Interactivos con `ipympl`:**
   - Para generar figuras interactivas sin volcar pesadas cadenas en Base64 al contexto, utiliza el backend `%matplotlib ipympl` (o `%matplotlib widget`).
   - Decláralo una única vez en la primera celda de configuración/imports del notebook.
   - Emplea siempre `fig, ax = plt.subplots()` y finaliza con `plt.show()`.
   - Cierra figuras previas con `plt.close('all')` o `plt.close(fig)` durante análisis iterativos para no saturar la memoria del kernel.
   - El servidor `data-shield-mcp` interceptará los widgets y gráficos retornando la confirmación `[Gráfico renderizado localmente]`, manteniendo el widget interactivo vivo en el archivo físico para el usuario.

---

## 2. Entorno Virtual y Manejo de Dependencias

- **Detección del Entorno:** El proyecto cliente debe contar con un entorno virtual `.venv` en su raíz que contenga `ipykernel` instalado. No intentes recrear o reactivar el entorno (`source .venv/bin/activate` o `virtualenv`).
- **Gestión con `uv`:** Si el análisis requiere paquetes adicionales que no estén presentes, instálalos ejecutando en la terminal:
  ```bash
  uv add <nombre_del_paquete>
  ```
- **Recarga en Memoria:** Tras instalar un paquete nuevo con `uv add`, ejecuta la herramienta `restart_kernel` para que las nuevas librerías queden disponibles para el proceso de Python en ejecución.

---

## 3. Catálogo de Herramientas MCP (`data-shield-mcp`)

### Tabla de Referencia Rápida de Invocación

| # | Herramienta | Parámetros Principales | Alias Compatibles | Ejemplo de Invocación JSON |
| :- | :--- | :--- | :--- | :--- |
| 1 | `create_notebook` | `filename="analisis.ipynb"` | `notebook_name`, `name` | `{"filename": "analisis.ipynb"}` o `{}` |
| 2 | `add_and_execute_cell` | `code: str` | `source`, `content` | `{"code": "import pandas as pd\ndf = pd.read_csv('../data/data.csv')\ndf"}` |
| 3 | `edit_and_execute_cell` | `cell_index: int`, `new_code: str` | `index`, `code`, `source`, `content` | `{"cell_index": 2, "new_code": "df.describe()"}` |
| 4 | `add_markdown_cell` | `markdown_text: str` | `text`, `content`, `source` | `{"markdown_text": "## 1. Análisis Exploratorio"}` |
| 5 | `edit_markdown_cell` | `cell_index: int`, `new_markdown_text: str` | `index`, `markdown_text`, `text`, `content`, `source` | `{"cell_index": 0, "new_markdown_text": "# Reporte Estadístico"}` |
| 6 | `insert_cell` | `cell_index: int`, `cell_type="code"`, `source: str`, `execute=False` | `index`, `type`, `code`, `content`, `markdown_text`, `text` | `{"cell_index": 1, "cell_type": "code", "source": "df.head()", "execute": true}` |
| 7 | `execute_cell` | `cell_index: int` | `index` | `{"cell_index": 3}` |
| 8 | `read_cell` | `cell_index: int` | `index` | `{"cell_index": 2}` |
| 9 | `read_notebook_status` | *(ninguno)* | — | `{}` |
| 10 | `move_cell` | `source_index: int`, `target_index: int` | `from_index`, `src`, `to_index`, `dst` | `{"source_index": 4, "target_index": 2}` |
| 11 | `delete_cell` | `cell_index: int` | `index` | `{"cell_index": 5}` |
| 12 | `clear_all_outputs` | *(ninguno)* | — | `{}` |
| 13 | `inspect_memory` | *(ninguno)* | — | `{}` |
| 14 | `run_scratchpad_code` | `code: str` | `source`, `content` | `{"code": "df.shape"}` |
| 15 | `restart_kernel` | *(ninguno)* | — | `{}` |
| 16 | `interrupt_kernel` | *(ninguno)* | — | `{}` |

> [!TIP]
> Todas las herramientas del servidor aceptan alias semánticos y toleran argumentos adicionales sin error (`extra='ignore'`). Para llamadas sin parámetros requeridos, basta con enviar un objeto JSON vacío `{}`.

---

El servidor expone estas herramientas especializadas divididas en tres categorías operativas:

### A. Herramientas de Inspección y Exploración Invisible (Scratchpad)
Estas herramientas permiten validar hipótesis y explorar datos sin modificar el archivo `.ipynb` ni ensuciar la narrativa final:

- **`run_scratchpad_code(code)`**:
  - *Cuándo usar:* Para exploración rápida preliminar, chequear tipos de datos, validar nombres de columnas (`df.columns`), contar nulos (`df.isna().sum()`) o ensayar una transformación compleja antes de plasmarla en el notebook.
  - *Comportamiento:* Ejecuta el código en el kernel activo pero **no lo añade** al `.ipynb`. Retorna la salida sanitizada.
- **`inspect_memory()`**:
  - *Cuándo usar:* Para conocer el estado de la memoria RAM del kernel sin tener que re-ejecutar celdas ni rastrear el historial de conversación.
  - *Comportamiento:* Devuelve un listado con los nombres de las variables de usuario definidas, su tipo de dato, tamaño en bytes y dimensión (`shape`).
- **`read_cell(cell_index)`**:
  - *Cuándo usar:* Para auditar o depurar una celda específica (su código y salida) cuando ocurre un error, en lugar de solicitar todo el notebook. Ahorra tokens significativamente.
  - *Parámetros:* `cell_index` (índice entero, basado en 0).
- **`read_notebook_status()`**:
  - *Cuándo usar:* Únicamente al inicio de una sesión o cuando se requiera revisar la estructura global completa del documento (número total de celdas, índice de cada celda y resumen de contenido).

### B. Herramientas de Edición y Estructuración del Notebook
Permiten construir un documento analítico reproducible y ordenado:

- **`create_notebook(filename="analisis.ipynb")`**:
  - *Cuándo usar:* Al inicio del trabajo para inicializar o activar un notebook en `notebooks/<filename>`. Si el archivo ya existe en disco, lo carga en memoria preservando sus celdas.
  - *Parámetros:* `filename` (nombre del archivo, por defecto `analisis.ipynb`).
- **`add_and_execute_cell(code)`**:
  - *Cuándo usar:* Para agregar un bloque de código funcional al final del documento y ejecutarlo de inmediato en el kernel.
  - *Comportamiento:* Añade la celda al `.ipynb`, almacena la salida completa en el archivo en disco y devuelve el resultado sanitizado al modelo.
- **`add_markdown_cell(markdown_text)`**:
  - *Cuándo usar:* Para añadir contexto metodológico, títulos de sección, explicaciones teóricas e interpretaciones de resultados al final del notebook.
- **`insert_cell(cell_index, cell_type, source, execute=False)`**:
  - *Cuándo usar:* Cuando sea necesario intercalar un bloque de código o explicación en una posición intermedia sin sobrescribir las celdas existentes (por ejemplo, un `import` olvidado al inicio o una celda Markdown explicativa intermedia).
  - *Parámetros:* `cell_index` (posición 0-based), `cell_type` (`"code"` o `"markdown"`), `source` (contenido), `execute` (booleano opcional para ejecutar celdas de código).
- **`edit_and_execute_cell(cell_index, new_code)`**:
  - *Cuándo usar:* Para corregir errores lógicos, refactorizar o actualizar el código de una celda existente.
  - *Comportamiento:* Sobrescribe el código de la celda en el índice especificado, lo re-ejecuta en el kernel y actualiza sus salidas en disco.
- **`edit_markdown_cell(cell_index, new_markdown_text)`**:
  - *Cuándo usar:* Para corregir o enriquecer la narrativa o redacción de una celda Markdown existente.
- **`move_cell(source_index, target_index)`**:
  - *Cuándo usar:* Para reorganizar la secuencia lógica del notebook sin reescribir ni eliminar celdas (ej. mover un gráfico de diagnóstico junto a su conclusión).
- **`delete_cell(cell_index)`**:
  - *Cuándo usar:* Para retirar celdas redundantes, fallidas o de descarte definitivo.

### C. Herramientas de Control del Ciclo de Vida del Kernel
Permiten gestionar la estabilidad y el estado de la sesión de ejecución:

- **`execute_cell(cell_index)`**:
  - *Cuándo usar:* Para volver a correr una celda sin alterar su contenido. Indispensable para repoblar variables en memoria tras un reinicio del kernel.
- **`interrupt_kernel()`**:
  - *Cuándo usar:* Cuando una ejecución se quede colgada o en bucle infinito (ej. convergencia lenta de un estimador), deteniendo el proceso sin perder las variables previamente calculadas en memoria.
- **`restart_kernel()`**:
  - *Cuándo usar:* Tras instalar nuevas dependencias con `uv add` o para purgar totalmente el estado de la RAM ante conflictos severos de variables.
- **`clear_all_outputs()`**:
  - *Cuándo usar:* Para limpiar todas las salidas del archivo `.ipynb` antes de una entrega final si se requiere un notebook limpio de ejecución.

---

## 4. Flujo de Trabajo Técnico Recomendado

Al abordar una solicitud de análisis o transformación de datos:

1. **Revisión del Contexto de Datos:**
   - Inspecciona los metadatos o esquemas existentes (ej. `contexto_datos.md`) para entender tipos y nombres de columnas antes de escribir código.
2. **Validación en Borrador (Scratchpad):**
   - Utiliza `run_scratchpad_code` para validar rutas, dimensiones (`.shape`), valores faltantes o probar transformaciones complejas.
3. **Estructuración Narrativa (Markdown):**
   - Antes de cada bloque importante de código, añade una celda Markdown (`add_markdown_cell` o `insert_cell`) documentando el objetivo analítico y la justificación técnica.
4. **Ejecución y Registro (Código):**
   - Agrega la celda con `add_and_execute_cell`.
   - Si la celda genera un DataFrame, finaliza con el nombre de la variable sola para que el servidor aplique la sanitización nativa.
   - Si genera un gráfico, asegura la configuración `%matplotlib ipympl` y finaliza con `plt.show()`.
5. **Depuración Focalizada:**
   - En caso de excepción o error, consulta el detalle usando `read_cell(cell_index)` y corrige con `edit_and_execute_cell(cell_index, nuevo_codigo)`. Evita solicitar el estado general del notebook a menos que sea indispensable.
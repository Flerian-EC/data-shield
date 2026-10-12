# Plugin Antigravity: `data-shield`

`data-shield` es un plugin para **Antigravity CLI** diseñado para permitir la manipulación, limpieza y análisis estadístico de datos en **Jupyter Notebooks (`.ipynb`)**.

Su propósito es proporcionar herramientas al agente de IA para interactuar con notebooks ejecutados en un kernel de Jupyter local, mientras actúa como un **escudo de tokens**: sanitiza salidas de datos masivas (truncando DataFrames) y filtra binarios pesados (como gráficos en Base64), protegiendo la ventana de contexto del modelo y preservando de manera íntegra el archivo `.ipynb` físico con sus figuras interactivas para revisión humana.

---

## 1. Configuración del Espacio de Trabajo del Usuario

El plugin está diseñado para operar sobre cualquier directorio de análisis de datos del usuario **de forma limpia y no invasiva**. Para preparar tu proyecto cliente de datos:

### 1.1 Aislamiento de Archivos (`.gitignore` y `.antigravityignore`)
Crea un archivo `.antigravityignore` en la raíz de tu proyecto de análisis para evitar que la IA indexe gigabytes de datos o lea directamente el JSON crudo de los notebooks:

```text
data/
.venv/
notebooks/
```

> **Regla de Oro:** Todo notebook debe residir obligatoriamente dentro de la carpeta `notebooks/`. Al ocultar este directorio a las herramientas genéricas del CLI, fuerzas a la IA a interactuar con los notebooks **exclusivamente mediante las herramientas del servidor `data-shield-mcp`**, garantizando la sanitización de salidas.

### 1.2 Entorno Virtual del Proyecto de Datos
En la raíz de tu proyecto de análisis, inicializa el entorno virtual con `uv` e instala `ipykernel` junto con las librerías analíticas esenciales:

```bash
uv add ipykernel pandas pyarrow openpyxl ipympl
```

> **Nota:** La presencia de `ipykernel` en el `.venv` del proyecto cliente es obligatoria. El servidor MCP detecta automáticamente este entorno virtual para levantar y comunicar el kernel de Jupyter en él.

### 1.3 Contexto Semántico de Datos (Opcional pero Recomendado)
Si dispones de metadatos o esquemas de las tablas en `data/`, crea un archivo `contexto_datos.md` en la raíz del proyecto cliente detallando nombres de variables, tipos, relaciones (1:1, 1:N) y particularidades estadísticas. El agente lo consultará antes de proponer transformaciones.

---

## 2. Modos de Uso en Antigravity CLI

Una vez registrado el plugin, cuentas con dos formas sencillas de interactuar con él:

### Opción A: Cambiar al Agente Especializado (`data-shield-agent`)
Puedes cambiar el agente principal por defecto al agente `data-shield-agent` ejecutando el comando interactivo en Antigravity CLI:
```text
/agents
```
En el menú de selección, escoge **`data-shield-agent`**. A partir de ese momento, el agente asumirá directamente el rol de **Científico de Datos Senior y experto en Estadística Matemática**, estructurando el notebook con narrativa metodológica en Markdown, validando hipótesis en borrador (`scratchpad`) y reportando interpretaciones técnicas fundamentadas.

### Opción B: Usar la Habilidad (`data-shield-skill`)
Si prefieres mantener el agente estándar, la habilidad se activa automáticamente cuando se requiere manipular o analizar notebooks, o puedes activarla explícitamente mediante el comando:
```text
/data-shield-skill
```
El agente aplicará el protocolo de las 16 herramientas MCP (`create_notebook`, `run_scratchpad_code`, `add_and_execute_cell`, etc.) para manipular el archivo en `notebooks/analisis.ipynb` garantizando el blindaje de tokens.

### Ciclo de Auditoría y Revisión Humana (*Human-in-the-Loop*)
1. Tras la ejecución, abre `notebooks/analisis.ipynb` en tu editor habitual (VS Code, JupyterLab).
2. Inspecciona de forma nativa los gráficos interactivos (zoom, paneo) y revisa las tablas completas.
3. Si requieres ajustes, pídelos iterativamente en la terminal de Antigravity CLI (ej. *"Cambia la imputación de valores faltantes por un modelo KNN e inserta la justificación teórica"*), y el agente editará las celdas precisas sin rehacer todo el documento.

---

## 3. Instalación y Registro del Plugin

### Paso 1: Configurar la ruta del servidor MCP
En el archivo `plugin/mcp_config.json`, indica la ruta absoluta donde se encuentra este repositorio:

```json
{
  "mcpServers": {
    "data-shield-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/ruta/absoluta/a/data-shield",
        "-m",
        "data_shield_mcp"
      ]
    }
  }
}
```

### Paso 2: Registrar el plugin en Antigravity CLI
Puedes habilitar el plugin mediante cualquiera de las siguientes vías:

- **Instalación interactiva con `/plugin`:**
  En la terminal de Antigravity CLI, escribe el comando `/plugin` para abrir el gestor de plugins y selecciona la opción para añadir o instalar un plugin local indicando la ruta a la carpeta `plugin` de este repositorio (`/ruta/absoluta/a/data-shield/plugin`).
- **Instalación mediante CLI:**
  ```bash
  agy plugin install /ruta/absoluta/a/data-shield/plugin
  ```
- **Instalación Global:**
  Copia o enlaza simbólicamente la carpeta `plugin` en `~/.gemini/antigravity-cli/plugins/data-shield`.
- **Instalación a Nivel de Proyecto:**
  Copia o enlaza la carpeta `plugin` en `.agents/plugins/data-shield` dentro del proyecto donde desees utilizarlo.

---

## 4. Arquitectura del Repositorio

El repositorio separa limpiamente la interfaz del plugin para Antigravity CLI del servidor MCP en Python:

```text
data-shield/
├── README.md                      # Documentación del plugin y guía de uso
├── pyproject.toml                 # Configuración de empaquetado del servidor MCP (hatchling)
├── data_shield_mcp/               # Servidor MCP (Python + MCP SDK v2)
│   ├── __init__.py
│   ├── __main__.py                # Entrypoint con inyección dinámica de sys.path
│   ├── kernel.py                  # Gestor asíncrono del kernel IPython y kernelspec
│   ├── sanitizer.py               # Filtro de sanitización y escudado de tokens
│   ├── server.py                  # Definición de las 16 herramientas MCP
│   └── context/                   # Especificaciones técnicas de referencia
└── plugin/                        # Paquete del Plugin para Antigravity CLI
    ├── plugin.json                # Manifiesto oficial del plugin
    ├── mcp_config.json            # Configuración de arranque del servidor MCP
    ├── agents/
    │   └── data-shield-agent.md   # Subagente (Científico de Datos Senior)
    └── skills/
        └── data-shield-skill/
            └── SKILL.md           # Habilidad técnica y catálogo de herramientas MCP
```

### Componentes del Plugin:
- **`plugin/plugin.json`**: Manifiesto que declara el plugin ante Antigravity CLI.
- **`plugin/agents/data-shield-agent.md`**: Define el subagente (`subagent: true`, `mainAgent: true`) con el perfil de un Científico de Datos Senior, asegurando rigor estadístico, validación empírica y justificación teórica.
- **`plugin/skills/data-shield-skill/SKILL.md`**: Define la habilidad técnica procedural que enseña el catálogo de herramientas MCP, gestión de dependencias con `uv` y reglas de aislamiento de contexto.
- **`plugin/mcp_config.json`**: Expone el servidor `data-shield-mcp` para que Antigravity CLI lo inicialice de manera automática en segundo plano.

---

## 5. Mecanismo de Blindaje de Contexto (El "Escudo")

Cuando el modelo ejecuta código en el notebook a través del MCP, `data-shield-mcp` procesa las salidas en memoria:

1. **Gráficos Interactivos y Widgets (`ipympl`):**
   - El código en el notebook renderiza figuras interactivas con `%matplotlib ipympl` (`fig, ax = plt.subplots()`, `plt.show()`).
   - El servidor MCP intercepta los datos pesados en Base64 y JSON de widgets, guardándolos en el `.ipynb` para su visualización humana en el editor, pero devolviendo al modelo únicamente: `[Gráfico renderizado localmente]`.
2. **DataFrames y Tablas Masivas:**
   - Si se evalúa un DataFrame de Pandas (dejando la variable en la última línea), el sanitizador intercepta la tabla y trunca su salida a las 5 primeras y 5 últimas filas con un indicador intermedio (`... [X filas omitidas] ...`).
3. **Limpieza de Trazas de Error:**
   - Los mensajes de error y tracebacks son saneados eliminando códigos ANSI de color y anonimizando rutas del sistema operativo para ahorrar tokens y proteger la privacidad.
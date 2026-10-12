import re
from typing import Any

MAX_STREAM_CHARS = 1500
MAX_PLAIN_TEXT_CHARS = 3000
MAX_LINE_CHARS = 500
MAX_TOTAL_OUTPUT_CHARS = 6000
TRUNCATED_ROWS_HEAD = 5
TRUNCATED_ROWS_TAIL = 5


def _coerce_text(val: Any) -> str:
    """Convierte de forma segura strings, listas de líneas (estándar nbformat) o valores None en str."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        return "".join(_coerce_text(item) for item in val)
    return str(val)


def sanitize_outputs(outputs: list[dict]) -> str:
    """Recibe lista de outputs (formato nbformat) y retorna texto saneado para el LLM."""
    if not outputs:
        return ""

    sanitized_parts = []
    total_chars = 0

    for output in outputs:
        output_type = output.get("output_type")
        part = ""

        if output_type == "stream":
            text = _coerce_text(output.get("text", ""))
            part = _sanitize_stream(text)

        elif output_type in ("execute_result", "display_data"):
            data = output.get("data", {})
            if isinstance(data, dict):
                part = _sanitize_rich_output(data)

        elif output_type == "error":
            part = _sanitize_error(output)

        if not part:
            continue

        # Protección contra bucles con acumulación masiva de outputs
        if total_chars + len(part) > MAX_TOTAL_OUTPUT_CHARS:
            allowed = max(0, MAX_TOTAL_OUTPUT_CHARS - total_chars)
            if allowed > 100:
                sanitized_parts.append(part[:allowed] + "\n...[salidas adicionales omitidas para proteger el contexto]")
            else:
                sanitized_parts.append("...[salidas adicionales omitidas para proteger el contexto]")
            break

        sanitized_parts.append(part)
        total_chars += len(part) + 1

    return "\n".join(sanitized_parts)


def _sanitize_stream(text: str) -> str:
    if len(text) > MAX_STREAM_CHARS:
        remaining = len(text) - MAX_STREAM_CHARS
        return text[:MAX_STREAM_CHARS] + f"\n...[truncado: {remaining} caracteres restantes]"
    return text


def _sanitize_rich_output(data: dict) -> str:
    # 1. Si hay imagen, widget interactivo o librerías de gráficos pesadas (Plotly, Vega, Altair)
    rich_mime_prefixes = ("image/", "application/vnd.jupyter.widget-", "application/vnd.plotly", "application/vnd.vega")
    if any(any(k.startswith(prefix) for prefix in rich_mime_prefixes) for k in data):
        return "[Gráfico renderizado localmente]"

    # 2. Si hay HTML con imagen base64 embebida, svg, plotly o contenedor de widget
    html = _coerce_text(data.get("text/html", ""))
    if any(tag in html for tag in ("data:image/", "jupyter-widgets", "<svg", "plotly-graph-div", "class=\"plotly\"")):
        return "[Gráfico renderizado localmente]"

    # 3. Si hay representación de Canvas / Toolbar de Matplotlib en text/plain
    plain = _coerce_text(data.get("text/plain", ""))
    if plain.startswith("Canvas(toolbar="):
        return "[Gráfico renderizado localmente]"

    # 4. Si hay HTML (probable DataFrame), usar text/plain truncado
    if "text/html" in data:
        plain_df = _coerce_text(data.get("text/plain", "[DataFrame sin representación de texto]"))
        return _truncate_dataframe_text(plain_df)

    # 5. Fallback: usar text/plain
    if len(plain) > MAX_PLAIN_TEXT_CHARS:
        return plain[:MAX_PLAIN_TEXT_CHARS] + "\n...[truncado]"
    return plain


def _truncate_dataframe_text(text: str) -> str:
    """Trunca texto de DataFrame a primeras N + últimas N filas y aplica límite horizontal y global."""
    lines = text.splitlines()
    if len(lines) > (TRUNCATED_ROWS_HEAD + TRUNCATED_ROWS_TAIL + 3):
        header = lines[:TRUNCATED_ROWS_HEAD + 1]  # +1 por la cabecera
        footer = lines[-TRUNCATED_ROWS_TAIL:]
        total_rows = len(lines) - 1  # excluir cabecera (aprox)
        omitted = max(0, total_rows - TRUNCATED_ROWS_HEAD - TRUNCATED_ROWS_TAIL)
        lines = header + [f"... [{omitted} filas omitidas] ..."] + footer

    # Recorte horizontal (columnas ultra-anchas)
    capped_lines = []
    for line in lines:
        if len(line) > MAX_LINE_CHARS:
            capped_lines.append(line[:MAX_LINE_CHARS] + " ...[columnas truncadas]")
        else:
            capped_lines.append(line)

    result = "\n".join(capped_lines)
    if len(result) > MAX_PLAIN_TEXT_CHARS:
        result = result[:MAX_PLAIN_TEXT_CHARS] + "\n...[DataFrame truncado por límite de caracteres]"
    return result


def _sanitize_error(output: dict) -> str:
    """Conserva el traceback completo pero limpia rutas absolutas del sistema y recorta si es excesivo."""
    ename = _coerce_text(output.get("ename", "Error"))
    evalue = _coerce_text(output.get("evalue", ""))
    traceback_lines = output.get("traceback") or []

    # Limpiar secuencias ANSI de color
    clean_tb = []
    for line in traceback_lines:
        line_str = _coerce_text(line)
        clean_line = re.sub(r'\x1b\[[0-9;]*m', '', line_str)
        # Limpiar rutas absolutas del sistema en Linux, macOS y Windows
        clean_line = re.sub(r'/home/[^/]+/', '~/', clean_line)
        clean_line = re.sub(r'/Users/[^/]+/', '~/', clean_line)
        clean_line = re.sub(r'[A-Za-z]:\\Users\\[^\\]+\\', r'~\\', clean_line)
        clean_tb.append(clean_line)

    if not clean_tb:
        return f"{ename}: {evalue}"
    result = f"{ename}: {evalue}\n" + "\n".join(clean_tb)
    if len(result) > MAX_PLAIN_TEXT_CHARS:
        result = result[:MAX_PLAIN_TEXT_CHARS] + "\n...[traceback truncado]"
    return result

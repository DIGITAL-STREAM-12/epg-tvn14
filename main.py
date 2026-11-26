from datetime import datetime, timedelta, timezone
from io import BytesIO
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from PIL import Image, ImageFilter, ImageOps
import pytesseract
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

URL_PROGRAMACION = "https://www.tvn14.com/parrilla-de-programacion/"
TZ_CR = timezone(timedelta(hours=-6))
CHANNEL_ID = "tvn14.cr"
CHANNEL_NAME = "TVN Canal 14"
ARCHIVO_SALIDA = "epg_tvn14_diario.xml"

DIAS_SEMANA = [
    "Lunes",
    "Martes",
    "Miércoles",
    "Jueves",
    "Viernes",
    "Sábado",
    "Domingo",
]

PATRON_HORA = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?\s*m\.?|p\.?\s*m\.?|m\.?\s*d\.?|am|pm|md|m)?(?!\w)",
    re.IGNORECASE,
)


def normalizar_texto(texto: str) -> str:
    """Convierte a minúsculas, colapsa espacios y elimina tildes."""
    limpio = re.sub(r"\s+", " ", texto).strip().lower()
    return "".join(
        c
        for c in unicodedata.normalize("NFD", limpio)
        if unicodedata.category(c) != "Mn"
    )


def normalizar_ocr_linea(linea: str) -> str:
    """Corrige separadores comunes de OCR en horas (ej. 06.00 a.m. -> 06:00 a.m.)."""
    linea = re.sub(
        r"\b(\d{1,2})\.(\d{2})\s*(?=(?:a\.?\s*m\.?|p\.?\s*m\.?|m\.?\s*d\.?|am|pm|md|-|–|—|\s+a\b))",
        r"\1:\2 ",
        linea,
        flags=re.IGNORECASE,
    )
    linea = re.sub(r"^\s*(\d{1,2})\.(\d{2})\b", r"\1:\2", linea)
    return re.sub(r"\s+", " ", linea).strip()


def parsear_hora_24h(
    texto_hora: str, periodo_forzado: str | None = None
) -> tuple[int, int] | None:
    """Convierte texto de hora (12h o 24h) a (hora_24, minuto)."""
    m = PATRON_HORA.search(texto_hora.strip())
    if not m:
        return None

    hora = int(m.group(1))
    min_str = m.group(2)
    periodo_raw = m.group(3) or periodo_forzado

    if min_str is None and not periodo_raw:
        return None

    minuto = int(min_str) if min_str is not None else 0
    if not (0 <= minuto <= 59):
        return None

    if not periodo_raw:
        return (hora, minuto) if 0 <= hora <= 23 else None

    periodo = re.sub(r"[\s.]", "", periodo_raw).lower()
    if periodo == "md" or (periodo == "m" and min_str is not None):
        if hora != 12:
            return None
        hora_24 = 12
    elif periodo == "am":
        if not (1 <= hora <= 12):
            return None
        hora_24 = 0 if hora == 12 else hora
    elif periodo == "pm":
        if not (1 <= hora <= 12):
            return None
        hora_24 = hora if hora == 12 else hora + 12
    else:
        return None

    return (hora_24, minuto) if 0 <= hora_24 <= 23 else None


def extraer_horas_y_titulo(
    linea: str,
) -> tuple[tuple[int, int], tuple[int, int] | None, str] | None:
    """Extrae (inicio_24, fin_24_opcional, titulo) desde una línea de texto."""
    linea = normalizar_ocr_linea(linea)
    matches = [
        m
        for m in PATRON_HORA.finditer(linea)
        if parsear_hora_24h(m.group(0)) is not None
    ]
    if not matches:
        return None

    m_ini = matches[0]
    fin_24 = None
    corte_fin = m_ini.end()
    periodo_fin = None

    if len(matches) >= 2:
        entre = linea[m_ini.end() : matches[1].start()].strip().lower()
        if entre in ("-", "–", "—", "a", "al", "hasta", "/"):
            fin_24 = parsear_hora_24h(matches[1].group(0))
            corte_fin = matches[1].end()
            periodo_fin = matches[1].group(3)

    # Si el rango solo trae am/pm/md al final (ej: "1:00 - 2:00 p.m."), propagarlo al inicio
    if m_ini.group(3) is None and periodo_fin:
        p_norm = re.sub(r"[\s.]", "", periodo_fin).lower()
        h_ini_raw = int(m_ini.group(1))
        h_fin_raw = int(matches[1].group(1))
        if p_norm == "md" and 1 <= h_ini_raw < 12:
            ini_24 = parsear_hora_24h(m_ini.group(0), "am")
        elif p_norm in ("am", "pm") and 1 <= h_ini_raw <= 12:
            if p_norm == "pm" and h_ini_raw > h_fin_raw and h_ini_raw != 12:
                ini_24 = parsear_hora_24h(m_ini.group(0), "am")
            else:
                ini_24 = parsear_hora_24h(m_ini.group(0), periodo_fin)
        else:
            ini_24 = parsear_hora_24h(m_ini.group(0))
    else:
        ini_24 = parsear_hora_24h(m_ini.group(0))

    if not ini_24:
        return None

    resto = (linea[: m_ini.start()] + " " + linea[corte_fin:]).strip(" -–—:|/\t\n")
    resto = re.sub(r"\s+", " ", resto).strip()
    return ini_24, fin_24, resto


def esperar_superar_sgcaptcha(page, timeout_seg: int = 45) -> None:
    """Espera a que Playwright resuelva el reto sg-captcha de SiteGround si aparece."""
    inicio = time.monotonic()
    while time.monotonic() - inicio < timeout_seg:
        try:
            titulo = page.title()
            contenido = page.content()
        except Exception:
            time.sleep(0.8)
            continue

        en_reto = (
            "Robot Challenge Screen" in titulo
            or "sgchallenge" in contenido
            or "/.well-known/sgcaptcha" in page.url
            or "/.well-known/captcha" in page.url
            or "Checking the site connection security" in contenido
        )
        if not en_reto and len(contenido) > 1500:
            return

        time.sleep(1.0)

    raise TimeoutError("Tiempo agotado esperando la resolución del reto anti-bot sg-captcha.")


def seleccionar_pestana_dia(page, dia_objetivo: str) -> str | None:
    """Busca y hace clic en la pestaña/botón del día actual. Devuelve aria-controls si existe."""
    clave_obj = normalizar_texto(dia_objetivo)
    abrev_obj = clave_obj[:3]

    resultado = page.evaluate(
        """
        ({ claveObj, abrevObj }) => {
            const normalizar = (s) => (s || "")
                .normalize("NFD")
                .replace(/[\\u0300-\\u036f]/g, "")
                .replace(/\\s+/g, " ")
                .trim()
                .toLowerCase();

            const esVisible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === "none" || st.visibility === "hidden" || st.opacity === "0") {
                    return false;
                }
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            const selectores = [
                "main button, main [role='tab'], main a, main .elementor-tab-title, main li, main label",
                "article button, article [role='tab'], article a, article .elementor-tab-title, article li, article label",
                "button, [role='tab'], .elementor-tab-title, a, li, label"
            ];

            for (const sel of selectores) {
                const candidatos = Array.from(document.querySelectorAll(sel));
                const match = candidatos.find((el) => {
                    if (!esVisible(el)) return false;
                    if (el.closest("header, nav, footer")) return false;
                    const txt = normalizar(el.innerText || el.textContent);
                    if (!txt || txt.length > 25) return false;
                    return txt === claveObj || txt === abrevObj || txt.startsWith(claveObj + " ");
                });
                if (match) {
                    match.scrollIntoView({ behavior: "instant", block: "center" });
                    match.click();
                    return {
                        clicked: true,
                        text: match.innerText,
                        ariaControls: match.getAttribute("aria-controls") || null,
                    };
                }
            }
            return { clicked: false, text: null, ariaControls: null };
        }
        """,
        {"claveObj": clave_obj, "abrevObj": abrev_obj},
    )

    if resultado and resultado.get("clicked"):
        page.wait_for_timeout(1200)
        return resultado.get("ariaControls")

    print(f"[Aviso] No se encontró pestaña explícita para '{dia_objetivo}'; se usará el panel visible.")
    return None


def localizar_panel_activo(page, aria_controls: str | None):
    """Devuelve el Locator del panel activo o del contenido principal."""
    if aria_controls:
        loc = page.locator(f"#{aria_controls}")
        if loc.count() > 0 and loc.first.is_visible():
            return loc.first

    selectores_activos = [
        ".elementor-tab-content.elementor-active",
        "[role='tabpanel']:not([hidden]):not([aria-hidden='true'])",
        ".tab-pane.active",
        ".ui-tabs-panel",
    ]
    for sel in selectores_activos:
        loc = page.locator(sel)
        for idx in range(loc.count()):
            item = loc.nth(idx)
            if item.is_visible():
                box = item.bounding_box()
                if box and box["width"] > 100 and box["height"] > 80:
                    return item

    for fallback in ("main", "article", ".entry-content"):
        loc = page.locator(fallback)
        if loc.count() > 0 and loc.first.is_visible():
            return loc.first

    return page.locator("body")


def ocr_desde_bytes_png(png_bytes: bytes) -> list[str]:
    """Preprocesa una captura PNG y ejecuta Tesseract OCR en español."""
    img = Image.open(BytesIO(png_bytes)).convert("L")
    ancho, alto = img.size
    img = img.resize((ancho * 2, alto * 2), Image.Resampling.LANCZOS)
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.SHARPEN)

    texto = pytesseract.image_to_string(img, lang="spa", config="--psm 6")
    return [re.sub(r"\s+", " ", l).strip() for l in texto.splitlines() if l.strip()]


def extraer_lineas_panel(page, panel) -> list[str]:
    """Extrae texto tanto de imágenes (vía OCR/alt) como de texto visible del panel."""
    lineas: list[str] = []

    panel.scroll_into_view_if_needed()
    page.wait_for_timeout(800)

    imgs = panel.locator("img")
    capturas_realizadas = 0

    for idx in range(imgs.count()):
        img_loc = imgs.nth(idx)
        if not img_loc.is_visible():
            continue

        img_loc.scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        box = img_loc.bounding_box()
        if not box or box["width"] < 150 or box["height"] < 80:
            continue

        alt = (img_loc.get_attribute("alt") or "").strip()
        if alt and extraer_horas_y_titulo(alt):
            lineas.append(alt)

        try:
            png_bytes = img_loc.screenshot(type="png")
            lineas_ocr = ocr_desde_bytes_png(png_bytes)
            lineas.extend(lineas_ocr)
            capturas_realizadas += 1
        except Exception as err:
            print(f"[Aviso] No se pudo capturar/leer la imagen #{idx}: {err}")

    if capturas_realizadas == 0:
        try:
            png_bytes = panel.screenshot(type="png")
            lineas.extend(ocr_desde_bytes_png(png_bytes))
        except Exception as err:
            print(f"[Aviso] No se pudo capturar el panel completo: {err}")

    try:
        texto_dom = panel.inner_text() or ""
        for l in texto_dom.splitlines():
            limpia = re.sub(r"\s+", " ", l).strip()
            if limpia:
                lineas.append(limpia)
    except Exception:
        pass

    return lineas


def obtener_parrilla_tvn14() -> list[dict]:
    """Navega con Chromium headless, selecciona el día actual, aplica OCR y construye la parrilla."""
    hoy = datetime.now(TZ_CR).replace(hour=0, minute=0, second=0, microsecond=0)
    dia_actual = DIAS_SEMANA[hoy.weekday()]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="es-CR",
            timezone_id="America/Costa_Rica",
            viewport={"width": 1366, "height": 900},
            device_scale_factor=2,
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        try:
            page.goto(URL_PROGRAMACION, wait_until="domcontentloaded", timeout=60000)
            esperar_superar_sgcaptcha(page, timeout_seg=45)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass

            aria_controls = seleccionar_pestana_dia(page, dia_actual)
            panel = localizar_panel_activo(page, aria_controls)
            lineas = extraer_lineas_panel(page, panel)
            url_final = page.url
            titulo_pag = page.title()
        finally:
            browser.close()

    dias_norm = {normalizar_texto(d) for d in DIAS_SEMANA}
    programas_crudos = []
    vistos = set()

    i = 0
    while i < len(lineas):
        linea = lineas[i]
        if normalizar_texto(linea) in dias_norm:
            i += 1
            continue

        parsed = extraer_horas_y_titulo(linea)
        if parsed:
            ini_24, fin_24, titulo = parsed
            if len(titulo) < 2:
                for offset in range(1, 3):
                    if i + offset < len(lineas):
                        sig = lineas[i + offset]
                        if (
                            sig
                            and normalizar_texto(sig) not in dias_norm
                            and not extraer_horas_y_titulo(sig)
                        ):
                            titulo = sig
                            i += offset
                            break

            if titulo and len(titulo) >= 2:
                clave = (ini_24, titulo.lower())
                if clave not in vistos:
                    vistos.add(clave)
                    programas_crudos.append(
                        {
                            "inicio_24": ini_24,
                            "fin_24": fin_24,
                            "programa": titulo,
                        }
                    )
        i += 1

    programas_crudos.sort(key=lambda x: (x["inicio_24"][0], x["inicio_24"][1]))

    if not programas_crudos:
        print(
            f"[Error] No se detectaron programas. URL='{url_final}', "
            f"Título='{titulo_pag}', Líneas OCR={lineas[:15]}"
        )
        return []

    resultado = []
    for idx, item in enumerate(programas_crudos):
        h_ini, m_ini = item["inicio_24"]
        dt_inicio = hoy.replace(hour=h_ini, minute=m_ini)

        if item["fin_24"]:
            h_fin, m_fin = item["fin_24"]
            dt_fin = hoy.replace(hour=h_fin, minute=m_fin)
            if dt_fin <= dt_inicio:
                dt_fin += timedelta(days=1)
        elif idx + 1 < len(programas_crudos):
            h_sig, m_sig = programas_crudos[idx + 1]["inicio_24"]
            dt_fin = hoy.replace(hour=h_sig, minute=m_sig)
            if dt_fin <= dt_inicio:
                dt_fin += timedelta(days=1)
        else:
            dt_fin = dt_inicio + timedelta(hours=1)

        resultado.append(
            {
                "channel_id": CHANNEL_ID,
                "programa": item["programa"],
                "inicio_xmltv": dt_inicio.strftime("%Y%m%d%H%M%S %z"),
                "fin_xmltv": dt_fin.strftime("%Y%m%d%H%M%S %z"),
            }
        )

    return resultado


def guardar_xmltv(programas: list[dict], ruta_archivo: str = ARCHIVO_SALIDA) -> None:
    tv = ET.Element(
        "tv",
        {
            "generator-info-name": "TVN14-Daily-EPG",
            "source-info-url": URL_PROGRAMACION,
        },
    )

    canal = ET.SubElement(tv, "channel", {"id": CHANNEL_ID})
    display = ET.SubElement(canal, "display-name", {"lang": "es"})
    display.text = CHANNEL_NAME

    for item in programas:
        prog = ET.SubElement(
            tv,
            "programme",
            {
                "start": item["inicio_xmltv"],
                "stop": item["fin_xmltv"],
                "channel": item["channel_id"],
            },
        )
        title = ET.SubElement(prog, "title", {"lang": "es"})
        title.text = item["programa"]

    if hasattr(ET, "indent"):
        ET.indent(tv, space="  ")

    tree = ET.ElementTree(tv)
    tree.write(ruta_archivo, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    print("Obteniendo programación diaria de TVN Canal 14...")
    parrilla = obtener_parrilla_tvn14()
    if not parrilla:
        print("No se pudo generar el EPG. Revisa las líneas OCR registradas arriba.")
        sys.exit(1)

    guardar_xmltv(parrilla, ARCHIVO_SALIDA)
    print(f"EPG generado con {len(parrilla)} programas en '{ARCHIVO_SALIDA}'.")

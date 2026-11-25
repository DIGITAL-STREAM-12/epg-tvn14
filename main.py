from datetime import datetime, timedelta, timezone
import json
import re
from typing import Optional
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import requests

URLS_DIAS = {
    "lunes": "https://www.tvn14.com/parrilla-de-programacion/",
    "martes": "https://www.tvn14.com/parrilla-de-programacion-martes/",
    "miercoles": "https://www.tvn14.com/parrilla-de-programacion-miercoles/",
    "jueves": "https://www.tvn14.com/parrilla-de-programacion-jueves/",
    "viernes": "https://www.tvn14.com/parrilla-de-programacion-viernes/",
    "sabado": "https://www.tvn14.com/parrilla-de-programacion-sabado/",
    "domingo": "https://www.tvn14.com/parrilla-de-programacion-domingo/",
}

DIAS_SEMANA = [
    "lunes",
    "martes",
    "miercoles",
    "jueves",
    "viernes",
    "sabado",
    "domingo",
]

TZ_CR = timezone(timedelta(hours=-6))
CHANNEL_ID = "tvn14.cr"
CHANNEL_NAME = "TVN Canal 14"

FRANJAS = {"mañana", "tarde", "noche"}
PATRON_HORA = re.compile(
    r"\b(\d{1,2}:\d{2})\s*(am|pm|a\.m\.|p\.m\.|m|md)\b", re.IGNORECASE
)


def parsear_hora_24h(texto_hora: str) -> Optional[tuple[int, int]]:
    """Convierte '6:00 am', '12:00 m', '12:00 md' o '8:30 pm' a (hora_24, minuto)."""
    m = PATRON_HORA.search(texto_hora.strip())
    if not m:
        return None

    hh_mm, periodo = m.group(1), m.group(2).lower().replace(".", "")
    hora, minuto = map(int, hh_mm.split(":"))

    if periodo in ("m", "md"):
        hora_24 = 12 if hora == 12 else hora
    elif periodo == "am":
        hora_24 = 0 if hora == 12 else hora
    else: # pm
        hora_24 = hora if hora == 12 else hora + 12

    return (hora_24, minuto)


def lunes_de_semana_actual(referencia: Optional[datetime] = None) -> datetime:
    """Devuelve el lunes (00:00:00, -06:00) de la semana correspondiente a la fecha dada."""
    actual = referencia or datetime.now(TZ_CR)
    lunes = actual - timedelta(days=actual.weekday())
    return lunes.replace(hour=0, minute=0, second=0, microsecond=0)


def fecha_para_dia_en_semana(dia: str, lunes_base: datetime) -> datetime:
    """Calcula la fecha exacta del día dentro de la semana iniciada en lunes_base."""
    indice = DIAS_SEMANA.index(dia)
    return lunes_base + timedelta(days=indice)


def _buscar_titulo_cercano(
    lineas: list[str], idx: int, hacia_atras: bool = True
) -> Optional[str]:
    desplazamientos = range(1, 5) if hacia_atras else range(1, 3)
    for step in desplazamientos:
        pos = idx - step if hacia_atras else idx + step
        if pos < 0 or pos >= len(lineas):
            break
        candidato = lineas[pos]
        if candidato.lower() in FRANJAS or PATRON_HORA.search(candidato):
            continue
        if re.fullmatch(r"[a-z0-9-]+", candidato) and "-" in candidato:
            continue
        return candidato
    return None


def obtener_parrilla_dia(dia: str, fecha_base: datetime) -> list[dict]:
    """Extrae y ordena la programación de un día asignándole fecha_base."""
    dia = dia.lower().strip()
    url = URLS_DIAS.get(dia)
    if not url:
        raise ValueError(f"Día no válido: {dia}")

    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ParrillaEPGBot/1.0)"},
        timeout=20,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    main = soup.find("main") or soup.body or soup

    lineas = [
        re.sub(r"\s+", " ", txt).strip()
        for txt in main.stripped_strings
        if re.sub(r"\s+", " ", txt).strip()
    ]

    franja_actual = "General"
    programas = []
    vistos = set()

    for i, linea in enumerate(lineas):
        baja = linea.lower()
        if baja in FRANJAS:
            franja_actual = linea.capitalize()
            continue

        if "/" in linea and PATRON_HORA.search(linea):
            partes = linea.split("/", 1)
            inicio = parsear_hora_24h(partes[0])
            fin = parsear_hora_24h(partes[1])
            if not inicio:
                continue

            titulo = _buscar_titulo_cercano(lineas, i, hacia_atras=True)
            if titulo:
                clave = (inicio, titulo.lower())
                if clave not in vistos:
                    vistos.add(clave)
                    programas.append(
                        {
                            "franja": franja_actual,
                            "inicio_24": inicio,
                            "fin_24": fin,
                            "programa": titulo,
                        }
                    )

        elif PATRON_HORA.fullmatch(linea):
            inicio = parsear_hora_24h(linea)
            if not inicio:
                continue
            if i + 2 < len(lineas) and "/" in lineas[i + 2]:
                continue

            titulo = _buscar_titulo_cercano(lineas, i, hacia_atras=False)
            if titulo:
                clave = (inicio, titulo.lower())
                if clave not in vistos:
                    vistos.add(clave)
                    programas.append(
                        {
                            "franja": franja_actual,
                            "inicio_24": inicio,
                            "fin_24": None,
                            "programa": titulo,
                        }
                    )

    programas.sort(key=lambda item: item["inicio_24"])

    resultado = []
    for idx, item in enumerate(programas):
        h_ini, m_ini = item["inicio_24"]
        dt_inicio = fecha_base.replace(hour=h_ini, minute=m_ini)

        if item["fin_24"]:
            h_fin, m_fin = item["fin_24"]
            dt_fin = fecha_base.replace(hour=h_fin, minute=m_fin)
            if dt_fin <= dt_inicio:
                dt_fin += timedelta(days=1)
        elif idx + 1 < len(programas):
            h_sig, m_sig = programas[idx + 1]["inicio_24"]
            dt_fin = fecha_base.replace(hour=h_sig, minute=m_sig)
        else:
            dt_fin = (fecha_base + timedelta(days=1)).replace(hour=0, minute=0)

        resultado.append(
            {
                "channel_id": CHANNEL_ID,
                "dia": dia,
                "franja": item["franja"],
                "programa": item["programa"],
                "inicio_iso": dt_inicio.isoformat(),
                "fin_iso": dt_fin.isoformat(),
                "inicio_xmltv": dt_inicio.strftime("%Y%m%d%H%M%S %z"),
                "fin_xmltv": dt_fin.strftime("%Y%m%d%H%M%S %z"),
            }
        )

    return resultado


def obtener_parrilla_semanal(
    lunes_base: Optional[datetime] = None,
) -> list[dict]:
    """Combina los 7 días de la semana en una sola lista cronológica."""
    base = lunes_base or lunes_de_semana_actual()
    semanal: list[dict] = []

    for dia in DIAS_SEMANA:
        fecha_dia = fecha_para_dia_en_semana(dia, base)
        try:
            items_dia = obtener_parrilla_dia(dia, fecha_dia)
            semanal.extend(items_dia)
        except requests.RequestException as exc:
            print(f"[Aviso] No se pudo obtener {dia} ({fecha_dia.date()}): {exc}")

    semanal.sort(key=lambda p: p["inicio_iso"])
    return semanal


def guardar_json_semanal(programas: list[dict], ruta_archivo: str) -> None:
    payload = {
        "canal": CHANNEL_NAME,
        "channel_id": CHANNEL_ID,
        "generado_en": datetime.now(TZ_CR).isoformat(),
        "total_programas": len(programas),
        "programacion": programas,
    }
    with open(ruta_archivo, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def guardar_xmltv_semanal(programas: list[dict], ruta_archivo: str) -> None:
    tv = ET.Element(
        "tv",
        {
            "generator-info-name": "TVN14-Weekly-EPG",
            "source-info-url": "https://www.tvn14.com/parrilla-de-programacion/",
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
                "channel": CHANNEL_ID,
            },
        )
        title = ET.SubElement(prog, "title", {"lang": "es"})
        title.text = item["programa"]

        category = ET.SubElement(prog, "category", {"lang": "es"})
        category.text = item["franja"]

    ET.indent(tv, space=" ")
    tree = ET.ElementTree(tv)
    tree.write(ruta_archivo, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    parrilla_semana = obtener_parrilla_semanal()
    guardar_xmltv_semanal(parrilla_semana, "epg_tvn14_semanal.xml")
    guardar_json_semanal(parrilla_semana, "epg_tvn14_semanal.json")
    print(
        f"Generado EPG semanal con {len(parrilla_semana)} programas en epg_tvn14_semanal.xml"
    )
    

from fastapi import FastAPI, UploadFile, File
import pdfplumber
import pandas as pd
import re
import os
from datetime import datetime
import tempfile

app = FastAPI()


# ===============================
#   EXTRACCION DE TEXTO
# ===============================

def extraer_texto(pdf_path: str) -> str:
    """Extrae texto del PDF usando pdfplumber."""
    texto = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            texto += page_text + "\n"
    return texto


def normalizar_linea(linea: str) -> str:
    """Limpia múltiples espacios y normaliza una línea."""
    linea = linea.strip()
    linea = re.sub(r"\s+", " ", linea)
    return linea


# ===============================
#   PROCESADOR PRINCIPAL
# ===============================

def procesar_factura(pdf_path: str):
    if not os.path.exists(pdf_path):
        return {"error": "No se encontró el archivo"}

    # 1) EXTRAER TEXTO
    texto = extraer_texto(pdf_path)

    # 2) NORMALIZAR LÍNEAS
    raw_lineas = [l for l in texto.split("\n") if l.strip()]
    lineas = [normalizar_linea(l) for l in raw_lineas]
    df = pd.DataFrame(lineas, columns=["linea"])

    # 3) TEXTO PLANO PARA REGEX
    texto_plano = " ".join(df["linea"].tolist())

    datos = {}

    # =====================================
    #      FECHA DE EMISIÓN
    # =====================================
    m_fecha = re.search(
        r"(?:Fecha(?:\s+de\s+Emisi[oó]n)?[:\s]+)(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        texto_plano,
        flags=re.IGNORECASE
    )
    if m_fecha:
        datos["fecha"] = m_fecha.group(1)

    # =====================================
    #      NUMERO DE FACTURA
    # =====================================
    m_fact = re.search(
        r"N[º°]?\s*([0-9]{4})\s*-\s*([0-9]{6,8})",
        texto_plano,
        flags=re.IGNORECASE
    )
    if m_fact:
        datos["nro_factura"] = f"{m_fact.group(1)}-{m_fact.group(2)}"

    # =====================================
    #      CUIT EMISOR
    # =====================================
    m_cuit = re.search(
        r"CUIT[:\s-]*([0-9\-]{11,13})",
        texto_plano,
        flags=re.IGNORECASE
    )
    if m_cuit:
        datos["cuit_emisor"] = m_cuit.group(1)

    # =====================================
    #      CAE
    # =====================================
    m_cae = re.search(
        r"CAE(?:\s*NRO)?[:\s]*([0-9]{10,})",
        texto_plano,
        flags=re.IGNORECASE
    )
    if m_cae:
        datos["cae"] = m_cae.group(1)

    # =====================================
    #      PROVEEDOR
    # =====================================
    proveedor = df["linea"][df["linea"].str.contains(
        r"S\.A\.|S\.R\.L|S\.A|SRL|Ltda",
        case=False,
        regex=True
    )]
    if not proveedor.empty:
        datos["proveedor"] = proveedor.iloc[0]
    else:
        datos["proveedor"] = df.iloc[0]["linea"]

    # =====================================
    #      TOTAL (siempre el mayor monto)
    # =====================================
    montos_brutos = re.findall(
        r"\b[0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}\b",
        texto_plano
    )

    if montos_brutos:
        montos_float = [
            float(m.replace(".", "").replace(",", "."))
            for m in montos_brutos
        ]
        datos["total"] = max(montos_float)

    # =====================================
    #      CAMPOS FIJOS
    # =====================================
    datos["deposito"] = 1
    datos["fecha_contable"] = datetime.today().strftime("%d/%m/%Y")

    return datos


# ===============================
#    ENDPOINT PUBLICO
# ===============================

@app.post("/procesar_factura/")
async def procesar_factura_api(file: UploadFile = File(...)):
    # Guardar PDF temporalmente
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    # Procesar el PDF
    resultado = procesar_factura(tmp_path)

    # Borrar archivo temporal
    os.remove(tmp_path)

    return resultado

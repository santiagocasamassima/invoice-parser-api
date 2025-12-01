from fastapi import FastAPI, UploadFile, File
import pdfplumber
import pandas as pd
import re
import os
import json
from datetime import datetime
import tempfile

app = FastAPI()


def extraer_texto(pdf_path: str) -> str:
    """
    Extrae el texto del PDF usando pdfplumber (mucho más preciso que pypdf
    para tablas, columnas y montos alineados).
    """
    texto = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            texto += page_text + "\n"
    return texto


def normalizar_linea(linea: str) -> str:
    """
    Limpia una línea: colapsa espacios múltiples, saca espacios al inicio/fin.
    """
    linea = linea.strip()
    linea = re.sub(r"\s+", " ", linea)
    return linea


def procesar_factura(pdf_path: str):
    # Leer PDF
    if not os.path.exists(pdf_path):
        print(f"⚠️ No se encontró el archivo en {pdf_path}")
        return {"datos_generales": {"error": "Archivo no encontrado"}}

    # 1) Extraer texto con pdfplumber
    texto = extraer_texto(pdf_path)

    # 2) Normalizar y dividir en líneas
    raw_lineas = [l for l in texto.split("\n") if l.strip()]
    lineas = [normalizar_linea(l) for l in raw_lineas]
    df = pd.DataFrame(lineas, columns=["linea"])

    # Texto "plano" para regex globales
    texto_plano = " ".join(df["linea"].tolist())

    datos_generales = {}

    # ----------------------------
    # PATRONES SOBRE TEXTO GLOBAL
    # ----------------------------

    # Fecha de emisión: "Fecha", "Fecha de Emisión", "FECHA"
    m_fecha = re.search(
        r"(?:Fecha(?:\s+de\s+Emisi[oó]n)?[:\s]+)(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        texto_plano,
        flags=re.IGNORECASE
    )
    if m_fecha:
        datos_generales["Fecha"] = m_fecha.group(1)

    # Número de factura: "Nº 0003 - 00010171", etc.
    # Tomamos la primera coincidencia de N° + 4 dígitos + "-" + 6–8 dígitos
    m_fact = re.search(
        r"N[º°]?\s*([0-9]{4})\s*-\s*([0-9]{6,8})",
        texto_plano,
        flags=re.IGNORECASE
    )
    if m_fact:
        datos_generales["Nro_Factura"] = f"{m_fact.group(1)}-{m_fact.group(2)}"

    # CUIT (11 dígitos, con o sin guiones). Tomamos el primero (emisor).
    m_cuit = re.search(
        r"CUIT[:\s-]*([0-9\-]{11,13})",
        texto_plano,
        flags=re.IGNORECASE
    )
    if m_cuit:
        datos_generales["CUIT"] = m_cuit.group(1)

    # CAE (10+ dígitos seguidos)
    m_cae = re.search(
        r"CAE(?:\s*NRO)?[:\s]*([0-9]{10,})",
        texto_plano,
        flags=re.IGNORECASE
    )
    if m_cae:
        datos_generales["CAE"] = m_cae.group(1)

    # Fecha de vencimiento / Vto CAE
    m_f_vto = re.search(
        r"(?:Fecha\s*(?:de\s*)?(?:Vencimiento|Vto\.?\s*CAE)[:\s]*)(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        texto_plano,
        flags=re.IGNORECASE
    )
    if m_f_vto:
        datos_generales["Fecha_vencimiento"] = m_f_vto.group(1)

    # ----------------------------
    # PROVEEDOR
    # ----------------------------
    proveedor = df["linea"][df["linea"].str.contains(
        r"S\.A\.|S\.R\.L|S\.A|SRL|Ltda",
        case=False,
        regex=True
    )]
    if not proveedor.empty:
        datos_generales["Proveedor"] = proveedor.iloc[0]
    else:
        # fallback → primera línea del PDF
        datos_generales["Proveedor"] = df.iloc[0]["linea"]

    # ----------------------------
    # CONDICIONES DE VENTA
    # ----------------------------
    idx = df.index[df["linea"].str.contains(
        r"Condicion(?:es)?\s*de\s*(Venta|Pago)",
        case=False,
        regex=True
    )]
    if not idx.empty:
        start = idx[0]
        condiciones_texto = []
        for i in range(start, len(df)):
            linea = df.loc[i, "linea"].strip()
            if re.search(r"(TOTAL|CAE|Factura|CUIT)", linea, re.IGNORECASE):
                break
            condiciones_texto.append(linea)

        condiciones_texto = " ".join(condiciones_texto)
        match_plazo = re.search(r"\d+\s*d[ií]as", condiciones_texto, re.IGNORECASE)
        if match_plazo:
            datos_generales["Condiciones_Venta"] = match_plazo.group(0)

    # ----------------------------
    # TOTAL: tomar SIEMPRE el mayor monto del PDF
    # ----------------------------
    # Formato típico AR: 1.234.567,89
    montos_brutos = re.findall(
        r"\b[0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}\b",
        texto_plano
    )

    if montos_brutos:
        montos_float = [
            float(m.replace(".", "").replace(",", "."))
            for m in montos_brutos
        ]
        total_mayor = max(montos_float)
        datos_generales["Total"] = total_mayor

    # ----------------------------
    # BONIFICACIÓN (si aplica)
    # ----------------------------
    bon_idx = df.index[df["linea"].str.contains(r"\bBON", case=False, regex=True)]
    if not bon_idx.empty:
        header_line = df.loc[bon_idx[0], "linea"]
        header_cols = re.split(r"\s{2,}", header_line)

        try:
            pos_bon = next(i for i, col in enumerate(header_cols) if "BON" in col.upper())

            for i in range(bon_idx[0] + 1, len(df)):
                first_item_line = df.loc[i, "linea"]
                item_cols = re.split(r"\s{2,}", first_item_line)
                if len(item_cols) > pos_bon:
                    bonificacion_val = item_cols[pos_bon]

                    if re.match(r"^\d+[.,]?\d*$", bonificacion_val):
                        datos_generales["Bonificacion"] = bonificacion_val.replace(",", ".")
                    break
        except StopIteration:
            pass

    # ----------------------------
    # CAMPOS FIJOS / ADICIONALES
    # ----------------------------
    datos_generales["Deposito"] = 1
    datos_generales["Fecha_Contable"] = datetime.today().strftime("%d/%m/%Y")
    datos_generales["Comprobante_electronico"] = "S"

    return {"datos_generales": datos_generales}


@app.post("/procesar_factura/")
async def procesar_factura_api(file: UploadFile = File(...)):
    # Guardamos el PDF temporalmente
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    # Procesamos
    resultado = procesar_factura(tmp_path)

    # Borramos archivo temporal
    os.remove(tmp_path)

    return resultado

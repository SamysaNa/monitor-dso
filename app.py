import streamlit as st
import pandas as pd
import numpy as np
import re
import io
import plotly.graph_objects as go
from datetime import datetime

# ==========================================
# CONFIGURACIÓN DE PÁGINA
# ==========================================
st.set_page_config(page_title="Monitor DSO | Cobranzas", layout="wide", page_icon="📈")

# ==========================================
# SISTEMA DE LOGIN Y ROLES
# ==========================================
if "role" not in st.session_state:
    st.session_state.role = None

if st.session_state.role is None:
    st.markdown("<h2 style='text-align: center;'>🔐 Acceso al Sistema DSO</h2>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        with st.form("login_form"):
            usuario = st.text_input("Usuario")
            password = st.text_input("Contraseña", type="password")
            submit = st.form_submit_button("Ingresar", use_container_width=True)
            
            if submit:
                if usuario == "admin" and password == "admin123":
                    st.session_state.role = "admin"
                    st.rerun()
                elif usuario == "visor" and password == "visor123":
                    st.session_state.role = "visor"
                    st.rerun()
                else:
                    st.error("Credenciales incorrectas")
    st.stop()

with st.sidebar:
    st.markdown(f"**Usuario:** {st.session_state.role.upper()}")
    if st.button("Cerrar Sesión"):
        st.session_state.role = None
        st.session_state.pop("df_base", None)
        st.rerun()

# ==========================================
# FUNCIONES DE PROCESAMIENTO
# ==========================================
def parse_amount(val):
    if pd.isna(val): return 0.0
    if isinstance(val, (int, float)): return float(val)
    s = str(val).strip()
    
    is_negative = '-' in s or (s.startswith('(') and s.endswith(')'))
    s = re.sub(r'[^\d,\.]', '', s) # Deja solo números y separadores
    if not s: return 0.0
    
    if ',' in s and '.' in s:
        if s.find('.') < s.find(','):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
        
    try:
        num = float(s)
        return -num if is_negative else num
    except:
        return 0.0

def clean_client_name(name):
    return re.sub(r'^\d+[-\s]*', '', str(name)).strip()

@st.cache_data
def process_excel(file):
    try:
        # header=2 salta las 2 primeras filas (código y vacía)
        df_raw = pd.read_excel(file, header=2)
    except Exception as e:
        return None, f"Error al leer el archivo Excel: {str(e)}", None

    cols_lower = [str(c).lower().strip() for c in df_raw.columns]
    
    col_sujeto = -1
    col_fecha = -1
    col_subdiario = -1
    col_importe = -1
    
    # Búsqueda estricta (agarra la primera coincidencia y no se pisa)
    for i, c in enumerate(cols_lower):
        if col_sujeto == -1 and ('sujeto' in c or 'cliente' in c): col_sujeto = i
        elif col_fecha == -1 and ('fecha' in c): col_fecha = i
        elif col_subdiario == -1 and ('subdiario' in c): col_subdiario = i
        elif col_importe == -1 and ('importe' in c or 'saldo' in c): col_importe = i
        
    if col_sujeto == -1 or col_fecha == -1 or col_subdiario == -1 or col_importe == -1:
        debug_info = pd.DataFrame({"Columnas Detectadas": list(df_raw.columns)})
        return None, "Error: No detecté las columnas correctas. Verificá los títulos de la Fila 3.", debug_info

    df_data = df_raw.iloc[:, [col_sujeto, col_fecha, col_subdiario, col_importe]].copy()
    df_data.columns = ['Sujeto_Original', 'Fecha', 'Subdiario', 'Importe']
    
    df_data = df_data.dropna(subset=['Sujeto_Original', 'Subdiario'])
    
    if not pd.api.types.is_datetime64_any_dtype(df_data['Fecha']):
        df_data['Fecha'] = pd.to_datetime(df_data['Fecha'], errors='coerce')
    df_data = df_data.dropna(subset=['Fecha']) 
    
    df_data['Importe_Num'] = df_data['Importe'].apply(parse_amount)
    df_data = df_data[df_data['Importe_Num'] != 0].copy()
    
    df_data['Sujeto'] = df_data['Sujeto_Original'].apply(clean_client_name)
    df_data['Subdiario'] = df_data['Subdiario'].astype(str).str.upper()
    
    df_clean = df_data[df_data['Subdiario'].str.contains('VTA|COB')].copy()
    
    if len(df_clean) == 0:
        debug_info = df_data.head(15).astype(str)
        return None, "Se leyeron las columnas pero los importes dieron 0. Revisá el diagnóstico.", debug_info
        
    return df_clean, "OK", None

def evaluate_client_fifo(df_client):
    df_client = df_client.sort_values(by='Fecha')
    invoice_queue = []
    collections = []
    total_billed = 0
    total_collected = 0
    
    for _, row in df_client.iterrows():
        fecha = row['Fecha']
        importe = row['Importe_Num']
        subdiario = row['Subdiario']
        
        if 'VTA' in subdiario:
            if importe > 0:
                total_billed += importe
                invoice_queue.append({'fecha': fecha, 'saldo': importe})
            else: 
                payment_rem = abs(importe)
                while invoice_queue and payment_rem > 0.001:
                    inv = invoice_queue[0]
                    apply_amt = min(inv['saldo'], payment_rem)
                    inv['saldo'] -= apply_amt
                    payment_rem -= apply_amt
                    if inv['saldo'] <= 0.001:
                        invoice_queue.pop(0)
                        
        elif 'COB' in subdiario:
            payment_rem = abs(importe)
            total_collected += payment_rem
            weighted_days = 0
            amount_applied = 0
            
            while invoice_queue and payment_rem > 0.001:
                inv = invoice_queue[0]
                apply_amt = min(inv['saldo'], payment_rem)
                diff_days = max(0, (fecha - inv['fecha']).days)
                
                inv['saldo'] -= apply_amt
                payment_rem -= apply_amt
                weighted_days += (diff_days * apply_amt)
                amount_applied += apply_amt
                
                if inv['saldo'] <= 0.001:
                    invoice_queue.pop(0)
            
            final_days = weighted_days / amount_applied if amount_applied > 0 else 0
            if amount_applied > 0:
                collections.append({'fecha': fecha, 'importe': abs(importe), 'dias': round(final_days), 'monto_aplicado': amount_applied})
                
    open_balance = sum(inv['saldo'] for inv in invoice_queue)
    return collections, total_billed, total_collected, open_balance

def calculate_ai_stats(collections):
    if len(collections) < 2: return None
    dias = [c['dias'] for c in collections]
    n = len(dias)
    x = np.arange(n)
    slope, _ = np.polyfit(x, np.array(dias), 1)
    
    mid = n // 2
    avg_first = np.mean(dias[:mid])
    avg_second = np.mean(dias[mid:])
    diff_pct = ((avg_second - avg_first) / avg_first * 100) if avg_first > 0 else 0
    max_peak = max(dias)
    
    if slope > 0.4 or diff_pct > 15:
        return {'avg_first': avg_first, 'avg_second': avg_second, 'diff_pct': diff_pct, 'max_peak': max_peak, 'slope': slope, 'color': "#EF4444", 'estado': "Desmejoró"}
    elif slope < -0.4 or diff_pct < -15:
        return {'avg_first': avg_first, 'avg_second': avg_second, 'diff_pct': diff_pct, 'max_peak': max_peak, 'slope': slope, 'color': "#10B981", 'estado': "Mejoró"}
    else:
        return {'avg_first': avg_first, 'avg_second': avg_second, 'diff_pct': diff_pct, 'max_peak': max_peak, 'slope': slope, 'color': "#F59E0B", 'estado': "Estable"}

def generate_excel(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Base_DSO', columns=['Sujeto_Original', 'Fecha', 'Subdiario', 'Importe_Num'])
        workbook = writer.book
        worksheet = writer.sheets['Base_DSO']
        
        header_format = workbook.add_format({'bold': True, 'text_wrap': True, 'valign': 'top', 'fg_color': '#1E293B', 'font_color': 'white', 'border': 1})
        money_format = workbook.add_format({'num_format': '$#,##0.00'})
        date_format = workbook.add_format({'num_format': 'dd/mm/yyyy'})
        
        for col_num, value in enumerate(['Sujeto_Original', 'Fecha', 'Subdiario', 'Importe_Num']):
            worksheet.write(0, col_num, value, header_format)
            
        worksheet.set_column('A:A', 30) 
        worksheet.set_column('B:B', 15, date_format) 
        worksheet.set_column('C:C', 15)
        worksheet.set_column('D:D', 20, money_format) 
    return output.getvalue()

# ==========================================
# INTERFAZ PRINCIPAL
# ==========================================
st.title("📊 Monitor de Días en la Calle (DSO) & Análisis de Crédito")

if st.session_state.role == "admin":
    uploaded_file = st.file_uploader("Cargar Asientos Contables (Excel)", type=["xlsx", "xls"])
else:
    st.info("Modo Visor: Esperando que el Administrador procese los datos.")
    uploaded_file = None 

if uploaded_file:
    if "last_uploaded_file" not in st.session_state or st.session_state.last_uploaded_file != uploaded_file.name:
        st.session_state.pop("df_base", None)
        st.session_state.last_uploaded_file = uploaded_file.name
        
    if "df_base" not in st.session_state:
        with st.spinner("Procesando movimientos contables..."):
            df, status, debug_df = process_excel(uploaded_file)
            
            if df is not None:
                st.session_state.df_base = df
                st.success(f"¡Archivo procesado con éxito! ({len(df)} comprobantes evaluados)")
                st.rerun() 
            else:
                st.error(status) 
                if debug_df is not None:
                    st.warning("🔍 MODO DIAGNÓSTICO: Columnas encontradas en el Excel.")
                    st.dataframe(debug_df)

if "df_base" in st.session_state:
    df = st.session_state.df_base
    clientes = sorted(df['Sujeto'].unique())
    
    if st.session_state.role == "admin":
        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            st.download_button(
                label="📥 Descargar Excel Formateado",
                data=generate_excel(df),
                file_name=f"Reporte_DSO_{datetime.now().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
        with col_btn2:
            st.button("☁️ Guardar en Google Sheets", use_container_width=True)

    tab1, tab2 = st.tabs(["📑 Detalle por Cliente", "📋 Cuadro Analítico Resumen"])
    
    with tab1:
        selected_client = st.selectbox("Seleccionar Cliente:", clientes)
        df_client = df[df['Sujeto'] == selected_client]
        
        # ---------------- DIAGNÓSTICO VISUAL ----------------
        with st.expander("🔍 Ver datos crudos de este cliente (¿Están bien los importes?)"):
            st.dataframe(df_client[['Fecha', 'Subdiario', 'Importe', 'Importe_Num']], use_container_width=True, hide_index=True)
        # ----------------------------------------------------
        
        colls, billed, collected, balance = evaluate_client_fifo(df_client)
        stats = calculate_ai_stats(colls)
        
        col1, col2, col3, col4 = st.columns(4)
        avg_days = int(np.average([c['dias'] for c in colls], weights=[c['monto_aplicado'] for c in colls])) if colls else 0
        col1.metric("Promedio Ponderado", f"{avg_days} días")
        col2.metric("Total Facturado", f"${billed:,.0f}")
        col3.metric("Total Cobrado", f"${collected:,.0f}")
        col4.metric("Saldo Abierto Estimado", f"${balance:,.0f}")
        
        if colls:
            st.markdown("### Evolución de Pagos")
            fig = go.Figure()
            fechas = [c['fecha'].strftime('%d/%m/%Y') for c in colls]
            dias = [c['dias'] for c in colls]
            fig.add_trace(go.Bar(x=fechas, y=dias, name="Días en Calle", marker_color="#6366f1"))
            fig.add_trace(go.Scatter(x=fechas, y=[avg_days]*len(dias), mode='lines', name="Promedio", line=dict(color='red', dash='dash')))
            fig.update_layout(height=350, margin=dict(l=0, r=0, t=30, b=0), plot_bgcolor='rgba(0,0,0,0)')
            st.plotly_chart(fig, use_container_width=True)

    with tab2:
        st.markdown("### Resumen de Cartera y Detalle de Comprobantes")
        orden = st.radio("Ordenar cuadro por:", ["Alfabético", "Mayor deterioro", "Mejor evolución"], horizontal=True)
        
        lista_clientes = []
        for c in clientes:
            df_c = df[df['Sujeto'] == c]
            c_colls, _, _, _ = evaluate_client_fifo(df_c)
            c_stats = calculate_ai_stats(c_colls)
            if c_stats:
                lista_clientes.append({'cliente': c, 'stats': c_stats, 'df': df_c})
                
        if orden == "Mayor deterioro":
            lista_clientes.sort(key=lambda x: x['stats']['diff_pct'], reverse=True)
        elif orden == "Mejor evolución":
            lista_clientes.sort(key=lambda x: x['stats']['diff_pct'])
            
        for item in lista_clientes:
            cliente = item['cliente']
            stats_c = item['stats']
            df_hist = item['df'].sort_values(by='Fecha', ascending=False)[['Fecha', 'Subdiario', 'Importe_Num']]
            df_hist.rename(columns={'Importe_Num': 'Importe'}, inplace=True)
            
            sign_var = "+" if stats_c['diff_pct'] > 0 else ""
            sign_slope = "+" if stats_c['slope'] > 0 else ""
            
            st.markdown(f"""
            <div style="border: 2px solid {stats_c['color']}; border-radius: 12px; padding: 15px 20px; background-color: #ffffff; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; box-shadow: 0 4px 6px rgba(0,0,0,0.05); color: #1e293b; margin-top: 15px;">
                <div style="font-weight: 700; font-size: 15px; flex: 1; min-width: 200px;">
                    {cliente}
                </div>
                <div style="flex: 3; display: flex; flex-wrap: wrap; justify-content: space-between; font-size: 13px; gap: 15px;">
                    <div style="background: #f8fafc; padding: 5px 10px; border-radius: 6px; border: 1px solid #e2e8f0;">Promedio: <b>{int(stats_c['avg_first'])}d ➔ {int(stats_c['avg_second'])}d</b></div>
                    <div style="background: #f8fafc; padding: 5px 10px; border-radius: 6px; border: 1px solid #e2e8f0;">Variación: <b style="color: {stats_c['color']};">{sign_var}{stats_c['diff_pct']:.1f}%</b></div>
                    <div style="background: #f8fafc; padding: 5px 10px; border-radius: 6px; border: 1px solid #e2e8f0;">Pico Máx: <b>{stats_c['max_peak']}d</b></div>
                    <div style="background: #f8fafc; padding: 5px 10px; border-radius: 6px; border: 1px solid #e2e8f0;">Tendencia: <b>{sign_slope}{stats_c['slope']:.2f}</b></div>
                </div>
            </div>
            """, unsafe_allow_html=True)
            
            with st.expander(f"Ver detalle de movimientos (VTA, COB, NC) de {cliente}"):
                df_hist['Fecha'] = df_hist['Fecha'].dt.strftime('%d/%m/%Y')
                df_hist['Importe'] = df_hist['Importe'].apply(lambda x: f"${x:,.2f}")
                st.dataframe(df_hist, use_container_width=True, hide_index=True)

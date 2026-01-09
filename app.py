import base64
import io
import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)

# Configuración para guardar archivos temporalmente
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Nombres fijos para los archivos temporales (para sobrescribir en cada uso)
FILENAME_TRIB = 'temp_tributos.xlsx'
FILENAME_CONT = 'temp_contribuyentes.xlsx'

# --- FUNCIONES DE LIMPIEZA (Mantenemos la lógica robusta) ---
def limpiar_tributos(df_raw):
    # Lógica de detección de cabecera
    header_idx = None
    for i, row in df_raw.iterrows():
        row_str = row.astype(str).values
        if 'Mes' in row_str and ('Amazonas' in row_str or 'AMAZONAS' in row_str):
            header_idx = i
            break
    
    if header_idx is None: header_idx = 1 # Fallback

    df_raw.columns = df_raw.iloc[header_idx]
    df = df_raw.iloc[header_idx+1:].copy()
    df.columns = [str(c).strip() for c in df.columns]
    
    if 'Mes' not in df.columns and 'Año' not in df.columns:
        df.rename(columns={df.columns[0]: 'Mes', df.columns[1]: 'Año'}, inplace=True)

    id_vars = ['Mes', 'Año']
    value_vars = [c for c in df.columns if c not in id_vars and 'Unnamed' not in c]
    
    df_long = df.melt(id_vars=id_vars, value_vars=value_vars, var_name='Departamento', value_name='Recaudacion')
    df_long['Recaudacion'] = pd.to_numeric(df_long['Recaudacion'], errors='coerce').fillna(0)
    df_long['Año'] = pd.to_numeric(df_long['Año'], errors='coerce')
    df_long = df_long.dropna(subset=['Año'])
    df_long['Departamento'] = df_long['Departamento'].str.upper().str.strip()
    return df_long

def limpiar_contribuyentes(df_raw):
    # Lógica de cabecera compleja
    years = df_raw.iloc[0].values
    months = df_raw.iloc[1].values
    new_cols = []
    current_year = None
    
    for i in range(len(years)):
        y = years[i]
        m = months[i]
        if pd.notna(y) and str(y).strip() not in ['nan', '']:
            current_year = y 
        if i == 0: new_cols.append('Departamento')
        else:
            if current_year and pd.notna(m): new_cols.append(f"{current_year}_{m}")
            else: new_cols.append(f"DROP_{i}")
    
    df = df_raw.iloc[2:].copy()
    df.columns = new_cols
    df = df[[c for c in df.columns if not c.startswith('DROP')]]
    
    df_long = df.melt(id_vars=['Departamento'], var_name='Year_Mes', value_name='Contribuyentes')
    df_long[['Año', 'Mes']] = df_long['Year_Mes'].str.split('_', expand=True)
    
    mapa_meses = {'Ene.': 'Enero', 'Feb.': 'Febrero', 'Mar.': 'Marzo', 'Abr.': 'Abril', 'May.': 'Mayo', 'Jun.': 'Junio', 'Jul.': 'Julio', 'Ago.': 'Agosto', 'Sep.': 'Septiembre', 'Set.': 'Septiembre', 'Oct.': 'Octubre', 'Nov.': 'Noviembre', 'Dic.': 'Diciembre'}
    df_long['Mes'] = df_long['Mes'].map(mapa_meses).fillna(df_long['Mes'])
    df_long['Contribuyentes'] = pd.to_numeric(df_long['Contribuyentes'], errors='coerce').fillna(1)
    df_long['Año'] = pd.to_numeric(df_long['Año'], errors='coerce')
    df_long['Departamento'] = df_long['Departamento'].str.upper().str.strip().replace({'LIMA METROPOLITANA': 'LIMA'})
    return df_long

def procesar_logica(df_trib, df_cont, anio_target):
    # Unir
    df_merged = pd.merge(df_trib, df_cont, on=['Departamento', 'Año', 'Mes'], how='inner')
    
    # Filtrar Año
    df_filtrado = df_merged[df_merged['Año'] == anio_target].copy()
    
    if df_filtrado.empty:
        # Si el año no existe, devolvemos vacío para manejar el error arriba
        return None

    # Agrupar
    df_ranking = df_filtrado.groupby('Departamento').agg({
        'Recaudacion': 'sum',
        'Contribuyentes': 'mean'
    }).reset_index()

    # Métrica: (Millones * 1e6) / Contribuyentes
    df_ranking['Soles_por_Contribuyente'] = (df_ranking['Recaudacion'] * 1_000_000) / df_ranking['Contribuyentes']
    df_ranking = df_ranking.sort_values('Soles_por_Contribuyente', ascending=False)
    
    return df_ranking

# --- RUTAS ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_files():
    if 'file_tributos' not in request.files or 'file_contribuyentes' not in request.files:
        return "Faltan archivos", 400
    
    f_trib = request.files['file_tributos']
    f_cont = request.files['file_contribuyentes']
    
    # Guardar archivos en disco para poder re-leerlos al cambiar el año
    path_trib = os.path.join(app.config['UPLOAD_FOLDER'], FILENAME_TRIB)
    path_cont = os.path.join(app.config['UPLOAD_FOLDER'], FILENAME_CONT)
    
    f_trib.save(path_trib)
    f_cont.save(path_cont)
    
    # Redirigir al dashboard con el año por defecto (2024 o 2025)
    return redirect(url_for('dashboard', year=2024))

@app.route('/dashboard')
def dashboard():
    anio = int(request.args.get('year', 2024))
    
    path_trib = os.path.join(app.config['UPLOAD_FOLDER'], FILENAME_TRIB)
    path_cont = os.path.join(app.config['UPLOAD_FOLDER'], FILENAME_CONT)
    
    if not os.path.exists(path_trib) or not os.path.exists(path_cont):
        return redirect(url_for('index')) # Si no hay archivos, volver al inicio

    try:
        # Cargar archivos (detectando si es xlsx o csv por el contenido, pandas lo maneja bien usualmente, pero forzamos lectura)
        # Nota: pd.read_excel lee ruta directa.
        # Asumimos que el usuario subió excels (.xlsx). Si subió CSV renombrados a .xlsx puede fallar, pero asumimos uso correcto.
        # Si tus archivos originales eran CSV, cambia read_excel por read_csv.
        
        # Para ser flexible, intentamos leer.
        try:
            df_trib_raw = pd.read_excel(path_trib, header=None)
        except:
            df_trib_raw = pd.read_csv(path_trib, header=None)
            
        try:
            df_cont_raw = pd.read_excel(path_cont, header=None)
        except:
            df_cont_raw = pd.read_csv(path_cont, header=None)

        # Limpiar
        df_trib = limpiar_tributos(df_trib_raw)
        df_cont = limpiar_contribuyentes(df_cont_raw)
        
        # Obtener lista de años disponibles para el combo box
        anios_disponibles = sorted(list(set(df_trib['Año'].unique()) & set(df_cont['Año'].unique())), reverse=True)
        
        # Procesar datos del año seleccionado
        df_resultado = procesar_logica(df_trib, df_cont, anio)
        
        if df_resultado is None:
            return f"No hay datos para el año {anio}. <a href='/dashboard?year={anios_disponibles[0]}'>Volver al último año disponible</a>"

        # --- GENERAR RECURSOS (Gráfico y Excel) ---
        
        # 1. Gráfico
        plt.figure(figsize=(11, 8))
        # Colores
        norm = plt.Normalize(df_resultado['Soles_por_Contribuyente'].min(), df_resultado['Soles_por_Contribuyente'].max())
        colors = plt.cm.viridis(norm(df_resultado['Soles_por_Contribuyente'].values))
        
        bars = plt.barh(df_resultado['Departamento'], df_resultado['Soles_por_Contribuyente'], color=colors)
        plt.xlabel('Soles por Contribuyente')
        plt.title(f'Ranking de Eficiencia - Año {anio}')
        plt.gca().invert_yaxis()
        plt.tight_layout()
        
        # Etiquetas
        for bar in bars:
            width = bar.get_width()
            plt.text(width, bar.get_y() + bar.get_height()/2, f' S/ {width:,.0f}', 
                     va='center', ha='left', fontsize=8)

        img = io.BytesIO()
        plt.savefig(img, format='png')
        img.seek(0)
        plot_b64 = base64.b64encode(img.getvalue()).decode()
        plt.close()

        # 2. Excel
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            df_resultado.to_excel(writer, index=False, sheet_name=f'Ranking {anio}')
        excel_buffer.seek(0)
        excel_b64 = base64.b64encode(excel_buffer.getvalue()).decode()

        # Tabla HTML
        tabla_html = df_resultado.to_html(classes='table table-sm table-hover', index=False, float_format="{:,.2f}".format)

        return render_template('result.html', 
                               plot_url=plot_b64, 
                               excel_data=excel_b64, 
                               tabla=tabla_html, 
                               anio_actual=anio,
                               lista_anios=anios_disponibles)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Error procesando: {str(e)} <br> <a href='/'>Volver</a>"

if __name__ == '__main__':
    app.run(debug=True)
import logging
import time
import random
import atexit
import uuid
import threading
import requests
import re
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Habilitar CORS para todos los orígenes y rutas
CORS(app, resources={r"/*": {"origins": "*"}})

# Sistema de jobs en memoria
jobs = {}
jobs_lock = threading.Lock()

# Almacenar HTML de respuestas para debugging
html_responses = {}

# Nota: No usamos instancias globales de Playwright porque cada thread necesita su propia instancia

# 2Captcha API Key
TWOCAPTCHA_API_KEY = os.getenv('TWOCAPTCHA_API_KEY', 'edb59aef0dc3b1176df998d3c56fa304')

def cleanup_old_jobs():
    """Limpia jobs antiguos (>10 minutos)"""
    with jobs_lock:
        cutoff = datetime.now() - timedelta(minutes=10)
        to_delete = [
            job_id for job_id, job in jobs.items()
            if job.get('created_at', datetime.now()) < cutoff
        ]
        for job_id in to_delete:
            del jobs[job_id]
            logger.info(f"🗑️ Job {job_id} limpiado")

def solve_recaptcha(site_key, page_url, action=None):
    """Resuelve reCAPTCHA v2 o v3 usando 2Captcha"""
    if not TWOCAPTCHA_API_KEY:
        logger.error("No se configuró TWOCAPTCHA_API_KEY")
        return None
    
    logger.info("🤖 Enviando CAPTCHA a 2Captcha...")
    
    try:
        # Preparar datos para envío
        data = {
            'key': TWOCAPTCHA_API_KEY,
            'method': 'userrecaptcha',
            'googlekey': site_key,
            'pageurl': page_url,
            'json': 1
        }
        
        # Para reCAPTCHA v3, agregar el parámetro action
        if action:
            data['action'] = action
            logger.info(f"📝 Resolviendo reCAPTCHA v3 con action: {action}")
        else:
            logger.info("📝 Resolviendo reCAPTCHA v2")
        
        # Enviar captcha
        response = requests.post('http://2captcha.com/in.php', data=data, timeout=10)
        
        result = response.json()
        if result.get('status') != 1:
            logger.error(f"Error enviando captcha: {result}")
            return None
        
        captcha_id = result.get('request')
        logger.info(f"✓ CAPTCHA enviado, ID: {captcha_id}")
        
        # Esperar resultado (máximo 2 minutos)
        for attempt in range(24):
            time.sleep(5)
            logger.info(f"Verificando CAPTCHA... intento {attempt + 1}/24")
            
            response = requests.get('http://2captcha.com/res.php', params={
                'key': TWOCAPTCHA_API_KEY,
                'action': 'get',
                'id': captcha_id,
                'json': 1
            }, timeout=10)
            
            result = response.json()
            if result.get('status') == 1:
                token = result.get('request')
                logger.info("✅ CAPTCHA resuelto!")
                return token
            elif result.get('request') == 'CAPCHA_NOT_READY':
                continue
            else:
                logger.error(f"Error obteniendo resultado: {result}")
                return None
        
        logger.error("⏱️ Timeout esperando resolución del CAPTCHA")
        return None
        
    except Exception as e:
        logger.error(f"Error en solve_recaptcha: {e}")
        return None

def query_sisben(page, cedula_str):
    """Consulta SISBEN y extrae datos personales"""
    try:
        logger.info("🌐 Consultando SISBEN...")
        page_url = 'https://reportes.sisben.gov.co/dnp_sisbenconsulta'
        
        page.goto(page_url, wait_until='networkidle', timeout=30000)
        time.sleep(random.uniform(2, 3))
        
        # Esperar formulario
        page.wait_for_selector('select#TipoID', state='visible', timeout=10000)
        time.sleep(random.uniform(1, 2))
        
        # Seleccionar tipo de documento: Cédula de Ciudadanía (value="3")
        logger.info("✍️ Seleccionando tipo de documento: Cédula de Ciudadanía")
        page.select_option('select#TipoID', '3')
        time.sleep(random.uniform(1, 2))
        
        # Ingresar documento
        logger.info(f"✍️ Ingresando documento: {cedula_str}")
        page.fill('input#documento', cedula_str)
        time.sleep(random.uniform(1, 2))
        
        # Resolver reCAPTCHA v3
        site_key = '6Lfh6kwcAAAAANT-kyprjG-m2yGmDmfOCvXinRE6'
        action = 'submit'
        
        logger.info("🤖 Resolviendo reCAPTCHA v3 para SISBEN...")
        captcha_token = solve_recaptcha(site_key, page_url, action=action)
        
        if not captcha_token:
            logger.error("❌ No se pudo resolver el CAPTCHA de SISBEN")
            return None
        
        # Inyectar token del captcha
        logger.info("💉 Inyectando token de CAPTCHA v3...")
        page.evaluate(f"""
            (function() {{
                // Crear input hidden para el token
                var tokenInput = document.createElement('input');
                tokenInput.type = 'hidden';
                tokenInput.name = 'g-recaptcha-response';
                tokenInput.value = '{captcha_token}';
                
                // Buscar el formulario y agregar el token
                var form = document.querySelector('form');
                if (form) {{
                    // Remover token anterior si existe
                    var existing = form.querySelector('input[name="g-recaptcha-response"]');
                    if (existing) {{
                        existing.remove();
                    }}
                    form.appendChild(tokenInput);
                }}
                
                // También intentar ejecutar grecaptcha si existe
                if (window.grecaptcha && window.grecaptcha.execute) {{
                    try {{
                        window.grecaptcha.execute('{site_key}', {{action: '{action}'}}).then(function(token) {{
                            var input = document.querySelector('input[name="g-recaptcha-response"]');
                            if (input) {{
                                input.value = token;
                            }}
                        }});
                    }} catch(e) {{
                        console.log('Error ejecutando grecaptcha:', e);
                    }}
                }}
            }})();
        """)
        
        time.sleep(2)
        
        # Enviar formulario
        logger.info("📤 Enviando formulario SISBEN...")
        try:
            with page.expect_navigation(wait_until='networkidle', timeout=30000):
                page.click('input#botonenvio')
        except PlaywrightTimeoutError:
            logger.warning("⚠️ Timeout en navegación SISBEN, verificando contenido...")
        
        time.sleep(3)
        
        # Extraer datos de la respuesta
        html = page.content()
        texto = page.inner_text('body')
        
        logger.info("📄 Parseando datos de SISBEN...")
        
        # Buscar datos personales en el HTML
        resultado_sisben = {
            "nombres": None,
            "apellidos": None,
            "tipo_documento": None,
            "numero_documento": None,
            "municipio": None,
            "departamento": None
        }
        
        try:
            # Buscar directamente en el HTML usando regex
            import re
            
            # Guardar HTML para debugging
            logger.info(f"📝 Longitud del HTML SISBEN: {len(html)} caracteres")
            logger.info(f"📝 Primeros 1000 caracteres del HTML: {html[:1000]}")
            
            # Función auxiliar para buscar campos de forma más flexible
            def buscar_campo(label, html_content):
                """Busca un campo por su etiqueta de forma flexible"""
                # Patrón 1: Buscar dentro de un div.row que contiene ambos elementos
                # <div class="row campo rounded">...<p class="etiqueta1" style="color:blue">Label:</p>...<p class="campo1">Value</p>...</div>
                pattern1 = rf'<div[^>]*class="[^"]*row[^"]*campo[^"]*"[^>]*>[\s\S]*?<p[^>]*class="[^"]*etiqueta1[^"]*"[^>]*style="color:blue"[^>]*>{re.escape(label)}:</p>[\s\S]*?<p[^>]*class="[^"]*campo1[^"]*"[^>]*>([\s\S]*?)</p>'
                match = re.search(pattern1, html_content, re.IGNORECASE)
                if match:
                    return match.group(1).strip()
                
                # Patrón 2: Buscar etiqueta seguida de campo en cualquier orden
                pattern2 = rf'<p[^>]*class="[^"]*etiqueta1[^"]*"[^>]*style="color:blue"[^>]*>{re.escape(label)}:</p>[\s\S]*?<p[^>]*class="[^"]*campo1[^"]*"[^>]*>([\s\S]*?)</p>'
                match = re.search(pattern2, html_content, re.IGNORECASE)
                if match:
                    return match.group(1).strip()
                
                # Patrón 3: Buscar con style antes de class
                pattern3 = rf'<p[^>]*style="color:blue"[^>]*class="[^"]*etiqueta1[^"]*"[^>]*>{re.escape(label)}:</p>[\s\S]*?<p[^>]*class="[^"]*campo1[^"]*"[^>]*>([\s\S]*?)</p>'
                match = re.search(pattern3, html_content, re.IGNORECASE)
                if match:
                    return match.group(1).strip()
                
                # Patrón 4: Buscar sin restricción de orden de atributos
                pattern4 = rf'<p[^>]*>{re.escape(label)}:</p>[\s\S]*?<p[^>]*class="[^"]*campo1[^"]*"[^>]*>([\s\S]*?)</p>'
                match = re.search(pattern4, html_content, re.IGNORECASE)
                if match:
                    return match.group(1).strip()
                
                return None
            
            # Método 1: Intentar con regex
            campos = {
                'nombres': buscar_campo('Nombres', html),
                'apellidos': buscar_campo('Apellidos', html),
                'tipo_documento': buscar_campo('Tipo de documento', html),
                'numero_documento': buscar_campo('Número de documento', html),
                'municipio': buscar_campo('Municipio', html),
                'departamento': buscar_campo('Departamento', html)
            }
            
            # Método 2: Si no se encontraron datos con regex, intentar con Playwright
            if not any(campos.values()):
                logger.info("🔍 No se encontraron datos con regex, intentando con Playwright...")
                try:
                    # Buscar todos los divs con clase "row campo"
                    rows = page.query_selector_all('div.row.campo')
                    logger.info(f"📊 Encontrados {len(rows)} divs con clase 'row campo'")
                    
                    for row in rows:
                        # Buscar la etiqueta dentro del row
                        etiqueta = row.query_selector('p.etiqueta1')
                        if etiqueta:
                            etiqueta_text = etiqueta.inner_text().strip()
                            # Buscar el campo correspondiente
                            campo_elem = row.query_selector('p.campo1')
                            if campo_elem:
                                valor = campo_elem.inner_text().strip()
                                valor_limpio = ' '.join(valor.split())
                                
                                # Mapear etiqueta a campo
                                if 'Nombres' in etiqueta_text:
                                    resultado_sisben['nombres'] = valor_limpio
                                    logger.info(f"✓ Nombres extraídos (Playwright): {valor_limpio}")
                                elif 'Apellidos' in etiqueta_text:
                                    resultado_sisben['apellidos'] = valor_limpio
                                    logger.info(f"✓ Apellidos extraídos (Playwright): {valor_limpio}")
                                elif 'Tipo de documento' in etiqueta_text:
                                    resultado_sisben['tipo_documento'] = valor_limpio
                                    logger.info(f"✓ Tipo de documento extraído (Playwright): {valor_limpio}")
                                elif 'Número de documento' in etiqueta_text:
                                    resultado_sisben['numero_documento'] = valor_limpio
                                    logger.info(f"✓ Número de documento extraído (Playwright): {valor_limpio}")
                                elif 'Municipio' in etiqueta_text:
                                    resultado_sisben['municipio'] = valor_limpio
                                    logger.info(f"✓ Municipio extraído (Playwright): {valor_limpio}")
                                elif 'Departamento' in etiqueta_text:
                                    resultado_sisben['departamento'] = valor_limpio
                                    logger.info(f"✓ Departamento extraído (Playwright): {valor_limpio}")
                except Exception as e:
                    logger.warning(f"⚠️ Error usando Playwright para extraer datos: {e}")
            else:
                # Procesar y limpiar los valores encontrados con regex
                for campo, valor in campos.items():
                    if valor:
                        # Limpiar espacios múltiples y saltos de línea
                        valor_limpio = ' '.join(valor.split())
                        resultado_sisben[campo] = valor_limpio
                        logger.info(f"✓ {campo.replace('_', ' ').title()} extraído: {valor_limpio}")
            
            # Verificar si se encontraron datos
            if any(resultado_sisben.values()):
                logger.info(f"✅ Datos SISBEN extraídos: {resultado_sisben}")
                return resultado_sisben
            else:
                logger.warning("⚠️ No se encontraron datos en SISBEN")
                # Verificar si hay mensaje de error
                if 'no se encontro' in texto.lower() or 'no existe' in texto.lower():
                    logger.info("📝 Cédula no encontrada en SISBEN")
                    return {"status": "not_found"}
                return None
                
        except Exception as e:
            logger.error(f"❌ Error parseando datos SISBEN: {e}", exc_info=True)
            return None
            
    except Exception as e:
        logger.error(f"❌ Error consultando SISBEN: {e}", exc_info=True)
        return None

def init_browser():
    """Inicializa Playwright y browser - cada thread necesita su propia instancia"""
    try:
        logger.info("🚀 Iniciando Playwright...")
        
        # Cada thread debe tener su propia instancia de Playwright
        playwright = sync_playwright().start()
        
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-gpu',
            ]
        )
        logger.info("✅ Browser iniciado")
        
        # Retornar tanto el browser como playwright para poder cerrarlos después
        return browser, playwright
        
    except Exception as e:
        logger.error(f"❌ Error iniciando browser: {e}")
        raise

# Nota: cleanup ya no es necesario porque cada thread limpia sus propios recursos
# atexit.register(cleanup)

def process_cedula_job(job_id, cedula_str):
    """Procesa una consulta de cédula en background"""
    context = None
    page = None
    browser = None
    playwright = None
    
    try:
        # Actualizar estado
        with jobs_lock:
            jobs[job_id]['status'] = 'processing'
            jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info(f"📋 Procesando job {job_id}: {cedula_str}")
        
        browser, playwright = init_browser()
        
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='es-CO',
            timezone_id='America/Bogota',
            viewport={'width': 1920, 'height': 1080},
        )
        
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['es-CO', 'es']});
            window.chrome = {runtime: {}};
        """)
        
        page = context.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,ico}", lambda route: route.abort())
        
        # PRIMERO: Consultar SISBEN
        logger.info("🔄 Iniciando consulta SISBEN (primera consulta)...")
        with jobs_lock:
            jobs[job_id]['status'] = 'querying_sisben'
            jobs[job_id]['message'] = 'Consultando SISBEN...'
            jobs[job_id]['updated_at'] = datetime.now()
        
        resultado_sisben = query_sisben(page, cedula_str)
        
        # Guardar resultado parcial de SISBEN para mostrar inmediatamente
        if resultado_sisben:
            with jobs_lock:
                jobs[job_id]['result'] = {
                    "status": "partial",
                    "mensaje": "Consulta SISBEN completada. Consultando Registraduría...",
                    "datos": {
                        "sisben": resultado_sisben,
                        "registraduria": None
                    }
                }
                jobs[job_id]['updated_at'] = datetime.now()
            logger.info("✅ Resultado parcial SISBEN guardado")
        
        # SEGUNDO: Consultar Registraduría
        logger.info("🔄 Iniciando consulta Registraduría (segunda consulta)...")
        with jobs_lock:
            jobs[job_id]['status'] = 'processing'
            jobs[job_id]['message'] = 'Consultando Registraduría...'
            jobs[job_id]['updated_at'] = datetime.now()
        
        page_url = 'https://wsp.registraduria.gov.co/censo/consultar'
        
        logger.info("🌐 Navegando a Registraduría...")
        page.goto(page_url, wait_until='networkidle', timeout=30000)
        
        # Esperar formulario
        page.wait_for_selector('input#nuip', state='visible', timeout=10000)
        time.sleep(random.uniform(1, 2))
        
        # Llenar cédula
        logger.info(f"✍️ Ingresando cédula: {cedula_str}")
        page.type('input#nuip', cedula_str, delay=random.randint(80, 150))
        
        time.sleep(random.uniform(1, 2))
        
        # Seleccionar tipo de elección - debe ser "LUGAR DE VOTACIÓN ACTUAL..." (value="-1")
        try:
            page.wait_for_selector('select#tipo', state='visible', timeout=5000)
            # Seleccionar la opción "LUGAR DE VOTACIÓN ACTUAL..." con value="-1"
            page.select_option('select#tipo', '-1')
            logger.info("✓ Elección seleccionada: LUGAR DE VOTACIÓN ACTUAL...")
        except Exception as e:
            logger.warning(f"Error seleccionando elección: {e}")
            # Intentar de forma alternativa
            try:
                page.evaluate("""
                    var select = document.getElementById('tipo');
                    if (select) {
                        select.value = '-1';
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                """)
                logger.info("✓ Elección seleccionada (método alternativo)")
            except Exception as e2:
                logger.error(f"Error en método alternativo de selección: {e2}")
        
        time.sleep(random.uniform(1, 2))
        
        # Resolver reCAPTCHA
        site_key = '6LcthjAgAAAAAFIQLxy52074zanHv47cIvmIHglH'
        
        with jobs_lock:
            jobs[job_id]['status'] = 'solving_captcha'
            jobs[job_id]['message'] = 'Resolviendo CAPTCHA con 2Captcha...'
            jobs[job_id]['updated_at'] = datetime.now()
        
        captcha_token = solve_recaptcha(site_key, page_url)
        
        if not captcha_token:
            logger.error("❌ No se pudo resolver el CAPTCHA")
            with jobs_lock:
                jobs[job_id]['status'] = 'captcha_failed'
                jobs[job_id]['result'] = {
                    "status": "error",
                    "mensaje": "No se pudo resolver el CAPTCHA. Verifica tu saldo en 2Captcha.",
                    "cedula": cedula_str
                }
                jobs[job_id]['updated_at'] = datetime.now()
            return
        
        # Inyectar token del captcha en el textarea
        logger.info("💉 Inyectando token de CAPTCHA...")
        page.evaluate(f"""
            var textarea = document.getElementById('g-recaptcha-response');
            if (textarea) {{
                textarea.innerHTML = '{captcha_token}';
                textarea.value = '{captcha_token}';
            }}
        """)
        
        time.sleep(1)
        
        # Enviar formulario
        logger.info("📤 Enviando formulario...")
        try:
            with page.expect_navigation(wait_until='networkidle', timeout=30000):
                page.click('input[type="submit"]#enviar')
        except PlaywrightTimeoutError:
            logger.warning("⚠️ Timeout en navegación, verificando contenido...")
        
        time.sleep(3)
        
        # Obtener contenido
        texto = page.inner_text('body')
        html = page.content()
        
        # Guardar HTML para debugging
        with jobs_lock:
            html_responses[job_id] = html
        
        logger.info("📄 Contenido obtenido, parseando datos...")
        logger.info(f"📝 Longitud del HTML: {len(html)} caracteres")
        logger.info(f"📝 Primeros 500 caracteres del texto: {texto[:500]}")
        
        # Detectar errores
        texto_lower = texto.lower()
        if 'no se encontro' in texto_lower or 'no existe' in texto_lower or 'no se encontró' in texto_lower:
            with jobs_lock:
                jobs[job_id]['status'] = 'not_found'
                jobs[job_id]['result'] = {
                    "status": "not_found",
                    "mensaje": "Cédula no encontrada en el sistema",
                    "cedula": cedula_str
                }
                jobs[job_id]['updated_at'] = datetime.now()
            return
        
        # Parsear datos de la tabla
        resultado = {
            "nuip": None,
            "departamento": None,
            "municipio": None,
            "puesto": None,
            "direccion": None,
            "mesa": None
        }
        
        try:
            # Método 1: Buscar tabla con estructura de headers y datos
            logger.info("🔍 Buscando datos por selectores específicos...")
            
            # Buscar tabla con datos
            tabla = page.query_selector('table')
            if tabla:
                logger.info("📊 Tabla encontrada, extrayendo datos...")
                filas = tabla.query_selector_all('tr')
                
                # Buscar la fila de headers
                headers = []
                data_row_index = -1
                
                for idx, fila in enumerate(filas):
                    celdas = fila.query_selector_all('td, th')
                    if len(celdas) >= 6:  # Debe tener al menos 6 columnas
                        # Verificar si es fila de headers (contiene palabras como NUIP, DEPARTAMENTO, etc.)
                        primera_celda = celdas[0].inner_text().strip().upper()
                        if 'NUIP' in primera_celda or 'DEPARTAMENTO' in primera_celda:
                            # Es la fila de headers
                            headers = [celda.inner_text().strip() for celda in celdas]
                            logger.info(f"✓ Headers encontrados: {headers}")
                            # La siguiente fila debería tener los datos
                            if idx + 1 < len(filas):
                                data_row_index = idx + 1
                            break
                
                # Si encontramos headers, extraer datos de la fila siguiente
                if headers and data_row_index >= 0 and data_row_index < len(filas):
                    fila_datos = filas[data_row_index]
                    celdas_datos = fila_datos.query_selector_all('td, th')
                    
                    if len(celdas_datos) >= len(headers):
                        # Mapear cada dato a su campo correspondiente
                        for i, header in enumerate(headers):
                            if i < len(celdas_datos):
                                valor = celdas_datos[i].inner_text().strip()
                                header_upper = header.upper()
                                
                                if 'NUIP' in header_upper or 'CÉDULA' in header_upper or 'CEDULA' in header_upper:
                                    resultado['nuip'] = valor
                                    logger.info(f"✓ NUIP extraído: {valor}")
                                elif 'DEPARTAMENTO' in header_upper:
                                    resultado['departamento'] = valor
                                    logger.info(f"✓ Departamento extraído: {valor}")
                                elif 'MUNICIPIO' in header_upper:
                                    resultado['municipio'] = valor
                                    logger.info(f"✓ Municipio extraído: {valor}")
                                elif 'PUESTO' in header_upper:
                                    resultado['puesto'] = valor
                                    logger.info(f"✓ Puesto extraído: {valor}")
                                elif 'DIRECCIÓN' in header_upper or 'DIRECCION' in header_upper:
                                    resultado['direccion'] = valor
                                    logger.info(f"✓ Dirección extraída: {valor}")
                                elif 'MESA' in header_upper:
                                    resultado['mesa'] = valor
                                    logger.info(f"✓ Mesa extraída: {valor}")
            
            # Método 1.5: Si no se encontró tabla estructurada, intentar parsear el texto directamente
            if not any([resultado['nuip'], resultado['departamento'], resultado['municipio']]):
                logger.info("🔍 Intentando parsear desde texto plano...")
                # Buscar el patrón específico de la tabla en el texto
                lineas = texto.split('\n')
                for i, linea in enumerate(lineas):
                    linea_clean = linea.strip()
                    # Buscar la línea que contiene "INFORMACIÓN DEL LUGAR DE VOTACIÓN"
                    if 'INFORMACIÓN DEL LUGAR DE VOTACIÓN' in linea_clean.upper():
                        # Las siguientes líneas deberían tener los datos
                        if i + 2 < len(lineas):
                            # Saltar línea de headers, tomar línea de datos
                            linea_datos = lineas[i + 2].strip()
                            logger.info(f"📝 Línea de datos encontrada: {linea_datos[:100]}")
                            
                            # Parsear los datos separados por espacios/tabs
                            # El formato es: NUIP DEPARTAMENTO MUNICIPIO PUESTO DIRECCIÓN MESA
                            partes = linea_datos.split('\t') if '\t' in linea_datos else linea_datos.split()
                            
                            # Si está separado por múltiples espacios, usar regex o split inteligente
                            if len(partes) < 6:
                                # Intentar split más inteligente (múltiples espacios)
                                partes = re.split(r'\s{2,}', linea_datos)
                            
                            if len(partes) >= 6:
                                resultado['nuip'] = partes[0].strip()
                                resultado['departamento'] = partes[1].strip() if len(partes) > 1 else None
                                resultado['municipio'] = partes[2].strip() if len(partes) > 2 else None
                                resultado['puesto'] = partes[3].strip() if len(partes) > 3 else None
                                resultado['direccion'] = partes[4].strip() if len(partes) > 4 else None
                                resultado['mesa'] = partes[5].strip() if len(partes) > 5 else None
                                logger.info(f"✓ Datos parseados desde texto: {resultado}")
                                break
            
            # Método 2: Si no encontramos en tabla, buscar en divs/listas
            if not any([resultado['nuip'], resultado['departamento'], resultado['municipio']]):
                logger.info("🔍 Buscando en estructura de divs/listas...")
                # Buscar elementos que contengan los datos
                elementos = page.query_selector_all('div, p, li, span')
                texto_completo = page.inner_text('body')
                lineas = texto_completo.split('\n')
                
                # Buscar patrones en el texto
                for i, linea in enumerate(lineas):
                    linea_clean = linea.strip()
                    if not linea_clean:
                        continue
                    
                    # Buscar NUIP/Cédula (número de 8-10 dígitos)
                    if not resultado['nuip']:
                        if linea_clean.isdigit() and 8 <= len(linea_clean) <= 10:
                            resultado['nuip'] = linea_clean
                            logger.info(f"✓ NUIP encontrado: {linea_clean}")
                    
                    # Buscar Departamento (palabras comunes de departamentos)
                    if not resultado['departamento']:
                        deptos = ['CUNDINAMARCA', 'ANTIOQUIA', 'VALLE', 'ATLÁNTICO', 'SANTANDER', 
                                 'BOLÍVAR', 'BOYACÁ', 'NARIÑO', 'CÓRDOBA', 'TOLIMA', 'CAUCA',
                                 'HUILA', 'RISARALDA', 'META', 'MAGDALENA', 'QUINDÍO', 'CAQUETÁ',
                                 'CASANARE', 'SUCRE', 'NORTE DE SANTANDER', 'CALDAS', 'LA GUAJIRA']
                        for depto in deptos:
                            if depto in linea_clean.upper():
                                resultado['departamento'] = linea_clean
                                logger.info(f"✓ Departamento encontrado: {linea_clean}")
                                break
                    
                    # Buscar Municipio (después del departamento)
                    if not resultado['municipio']:
                        municipios_comunes = ['BOGOTÁ', 'BOGOTA', 'MEDELLÍN', 'MEDELLIN', 'CALI', 
                                            'BARRANQUILLA', 'CARTAGENA', 'BUCARAMANGA', 'PEREIRA',
                                            'SANTA MARTA', 'IBAGUÉ', 'IBAGUE', 'MANIZALES', 'PASTO']
                        for mun in municipios_comunes:
                            if mun in linea_clean.upper() and linea_clean.upper() != resultado.get('departamento', '').upper():
                                resultado['municipio'] = linea_clean
                                logger.info(f"✓ Municipio encontrado: {linea_clean}")
                                break
                    
                    # Buscar Puesto de Votación
                    if not resultado['puesto']:
                        if any(palabra in linea_clean.upper() for palabra in ['IE ', 'INSTITUCIÓN', 'INSTITUCION', 
                                                                              'ESCUELA', 'COLEGIO', 'PUESTO', 
                                                                              'CENTRO', 'LUGAR DE VOTACIÓN']):
                            resultado['puesto'] = linea_clean
                            logger.info(f"✓ Puesto encontrado: {linea_clean}")
                    
                    # Buscar Dirección (contiene calles, números, etc.)
                    if not resultado['direccion']:
                        if any(palabra in linea_clean.upper() for palabra in ['CALLE', 'CRA', 'CARRERA', 
                                                                             'AVENIDA', 'AV.', 'AV ', 'DIAGONAL',
                                                                             'TRANSVERSAL', 'KRA', 'CL ', 'CR ']):
                            # Verificar que no sea muy larga y que tenga sentido
                            if 10 <= len(linea_clean) <= 150:
                                resultado['direccion'] = linea_clean
                                logger.info(f"✓ Dirección encontrada: {linea_clean[:50]}...")
                    
                    # Buscar Mesa (número pequeño, generalmente 1-3 dígitos)
                    if not resultado['mesa']:
                        if linea_clean.isdigit() and 1 <= len(linea_clean) <= 3:
                            # Verificar que no sea el NUIP
                            if linea_clean != resultado.get('nuip', ''):
                                resultado['mesa'] = linea_clean
                                logger.info(f"✓ Mesa encontrada: {linea_clean}")
            
            # Método 3: Buscar en estructura de datos más específica
            # Intentar encontrar elementos con clases o IDs específicos
            selectores_especificos = [
                ('#nuip', 'nuip'),
                ('#departamento', 'departamento'),
                ('#municipio', 'municipio'),
                ('#puesto', 'puesto'),
                ('#direccion', 'direccion'),
                ('#mesa', 'mesa'),
                ('.nuip', 'nuip'),
                ('.departamento', 'departamento'),
                ('.municipio', 'municipio'),
            ]
            
            for selector, campo in selectores_especificos:
                try:
                    elemento = page.query_selector(selector)
                    if elemento and not resultado[campo]:
                        valor = elemento.inner_text().strip()
                        if valor:
                            resultado[campo] = valor
                            logger.info(f"✓ {campo} encontrado por selector {selector}: {valor}")
                except:
                    pass
            
            # Log de lo que encontramos
            logger.info(f"📊 Datos extraídos: {resultado}")
            
        except Exception as e:
            logger.error(f"❌ Error parseando datos: {e}", exc_info=True)
        
        # Asegurar que tenga al menos el NUIP
        if not resultado['nuip']:
            resultado['nuip'] = cedula_str
        
        # Combinar resultados (SISBEN primero, Registraduría segundo)
        resultado_combinado = {
            "sisben": resultado_sisben if resultado_sisben else None,
            "registraduria": resultado
        }
        
        # Guardar resultado completo
        with jobs_lock:
            jobs[job_id]['status'] = 'completed'
            jobs[job_id]['result'] = {
                "status": "success",
                "datos": resultado_combinado
            }
            jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info(f"✅ Job {job_id} completado exitosamente")
        logger.info(f"📊 Datos SISBEN: {resultado_sisben}")
        logger.info(f"📊 Datos Registraduría: {resultado}")
        
    except Exception as e:
        logger.error(f"❌ Error en job {job_id}: {e}", exc_info=True)
        with jobs_lock:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['result'] = {
                "status": "error",
                "mensaje": str(e)
            }
            jobs[job_id]['updated_at'] = datetime.now()
    
    finally:
        if page:
            try:
                page.close()
            except:
                pass
        if context:
            try:
                context.close()
            except:
                pass
        if browser:
            try:
                browser.close()
            except:
                pass
        if playwright:
            try:
                playwright.stop()
            except:
                pass

@app.route('/health', methods=['GET'])
def health_check():
    cleanup_old_jobs()
    return jsonify({
        "status": "healthy",
        "jobs_activos": len(jobs),
        "captcha_configured": bool(TWOCAPTCHA_API_KEY),
        "api_key_preview": TWOCAPTCHA_API_KEY[:8] + "..." if TWOCAPTCHA_API_KEY else None,
        "timestamp": time.time()
    }), 200

@app.route('/consulta_cedula', methods=['POST'])
def consulta_cedula_async():
    """Crea un job asíncrono y retorna inmediatamente"""
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "mensaje": "No se envio JSON"}), 400
        
        cedula = data.get('cedula')
        if not cedula:
            return jsonify({"status": "error", "mensaje": "Falta cedula"}), 400
        
        cedula_str = str(cedula).strip()
        if not cedula_str.isdigit() or len(cedula_str) < 6 or len(cedula_str) > 10:
            return jsonify({"status": "error", "mensaje": "Cedula invalida"}), 400
        
        # Crear job
        job_id = str(uuid.uuid4())
        
        with jobs_lock:
            jobs[job_id] = {
                'cedula': cedula_str,
                'status': 'pending',
                'result': None,
                'created_at': datetime.now(),
                'updated_at': datetime.now()
            }
        
        # Iniciar procesamiento en background
        thread = threading.Thread(target=process_cedula_job, args=(job_id, cedula_str))
        thread.daemon = True
        thread.start()
        
        logger.info(f"🆕 Job {job_id} creado para {cedula_str}")
        
        # Retornar inmediatamente
        return jsonify({
            "status": "accepted",
            "job_id": job_id,
            "cedula": cedula_str,
            "mensaje": "Consulta iniciada. El proceso tomará 60-120 segundos (incluye 2 consultas con resolución de CAPTCHA).",
            "endpoints": {
                "status": f"/job/{job_id}",
                "result": f"/job/{job_id}/result"
            }
        }), 202
        
    except Exception as e:
        logger.error(f"Error creando job: {e}")
        return jsonify({"status": "error", "mensaje": str(e)}), 500

@app.route('/job/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Obtiene el estado de un job"""
    with jobs_lock:
        job = jobs.get(job_id)
    
    if not job:
        return jsonify({
            "status": "error",
            "mensaje": "Job no encontrado o expirado"
        }), 404
    
    response = {
        "job_id": job_id,
        "status": job['status'],
        "cedula": job['cedula'],
        "created_at": job['created_at'].isoformat(),
        "updated_at": job['updated_at'].isoformat()
    }
    
    if job.get('message'):
        response['mensaje'] = job['message']
    
    return jsonify(response), 200

@app.route('/job/<job_id>/result', methods=['GET'])
def get_job_result(job_id):
    """Obtiene el resultado de un job completado"""
    with jobs_lock:
        job = jobs.get(job_id)
    
    if not job:
        return jsonify({
            "status": "error",
            "mensaje": "Job no encontrado o expirado"
        }), 404
    
    status = job['status']
    
    # Si hay resultado parcial, retornarlo
    result = job.get('result')
    if result and result.get('status') == 'partial':
        return jsonify(result), 200
    
    if status in ['pending', 'processing', 'solving_captcha', 'querying_sisben']:
        return jsonify({
            "status": status,
            "mensaje": f"Job en proceso: {status}",
            "job_id": job_id,
            "nota": "El CAPTCHA puede tardar 30-60 segundos en resolverse"
        }), 202
    
    # Retornar resultado (completado, error, not_found, captcha_failed)
    return jsonify(job.get('result', {})), 200

@app.route('/job/<job_id>/html', methods=['GET'])
def get_job_html(job_id):
    """Obtiene el HTML completo de la respuesta para debugging"""
    with jobs_lock:
        html = html_responses.get(job_id)
    
    if not html:
        return jsonify({
            "status": "error",
            "mensaje": "HTML no disponible para este job"
        }), 404
    
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}

@app.route('/consultar', methods=['GET'])
def consultar_html():
    """Sirve la interfaz HTML para consultar cédulas"""
    html_path = os.path.join(os.path.dirname(__file__), 'consultar.html')
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        return html_content, 200, {'Content-Type': 'text/html; charset=utf-8'}
    except FileNotFoundError:
        return jsonify({"error": "Archivo consultar.html no encontrado"}), 404

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "servicio": "Registraduria API - 2Captcha",
        "version": "7.0",
        "captcha_service": "2Captcha",
        "captcha_configured": bool(TWOCAPTCHA_API_KEY),
        "interfaz_web": "GET /consultar",
        "endpoints": {
            "crear_consulta": "POST /consulta_cedula",
            "estado_job": "GET /job/{job_id}",
            "resultado_job": "GET /job/{job_id}/result",
            "health": "GET /health"
        },
        "ejemplo": {
            "paso_1": "POST /consulta_cedula con {cedula: '1087549965'}",
            "paso_2": "Guardar el job_id retornado",
            "paso_3": "Esperar 30-60 segundos",
            "paso_4": "GET /job/{job_id}/result para obtener datos"
        },
        "nota": "Cada consulta usa 1 crédito de 2Captcha (~$0.003 USD)"
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=10000)

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
from concurrent.futures import ThreadPoolExecutor

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

# Sistema de procesamiento multitarea
MAX_WORKERS = 15  # Número máximo de consultas simultáneas
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Nota: No usamos instancias globales de Playwright porque cada thread necesita su propia instancia

# 2Captcha API Key
TWOCAPTCHA_API_KEY = os.getenv('TWOCAPTCHA_API_KEY', 'edb59aef0dc3b1176df998d3c56fa304')

# Cargar user agents desde archivo
USER_AGENTS = []

def load_user_agents():
    """Carga user agents desde el archivo user_agents.txt"""
    global USER_AGENTS
    try:
        user_agents_file = os.path.join(os.path.dirname(__file__), 'user_agents.txt')
        if os.path.exists(user_agents_file):
            with open(user_agents_file, 'r', encoding='utf-8') as f:
                USER_AGENTS = [line.strip() for line in f if line.strip()]
            logger.info(f"✅ Cargados {len(USER_AGENTS)} user agents desde user_agents.txt")
        else:
            # User agent por defecto si no existe el archivo
            USER_AGENTS = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36']
            logger.warning("⚠️ Archivo user_agents.txt no encontrado, usando user agent por defecto")
    except Exception as e:
        logger.error(f"❌ Error cargando user agents: {e}")
        USER_AGENTS = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36']

def get_random_user_agent():
    """Retorna un user agent aleatorio"""
    if USER_AGENTS:
        return random.choice(USER_AGENTS)
    return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# Cargar user agents al iniciar
load_user_agents()

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
        
        # Optimizado: polling muy agresivo para v3 (suele resolverse rápido)
        # Para v3, empezamos con intervalos muy cortos
        for attempt in range(40):  # Más intentos con intervalos muy cortos
            # Intervalos ultra cortos al principio (v3 es muy rápido), luego normales
            if attempt < 10:
                sleep_time = 1  # Primeros 10 intentos: cada 1 segundo (muy agresivo)
            elif attempt < 20:
                sleep_time = 1.5  # Siguientes 10: cada 1.5 segundos
            elif attempt < 30:
                sleep_time = 2  # Siguientes 10: cada 2 segundos
            else:
                sleep_time = 3  # Resto: cada 3 segundos
            
            time.sleep(sleep_time)
            
            # Solo log cada 5 intentos para no saturar
            if attempt % 5 == 0 or attempt < 3:
                logger.info(f"Verificando CAPTCHA... intento {attempt + 1}/40")
            
            response = requests.get('http://2captcha.com/res.php', params={
                'key': TWOCAPTCHA_API_KEY,
                'action': 'get',
                'id': captcha_id,
                'json': 1
            }, timeout=10)
            
            result = response.json()
            if result.get('status') == 1:
                token = result.get('request')
                logger.info(f"✅ CAPTCHA resuelto! (intento {attempt + 1})")
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
    """Consulta SISBEN y extrae datos personales - Adaptado del enfoque JS sin CAPTCHA"""
    try:
        logger.info("🌐 Consultando SISBEN...")
        page_url = 'https://reportes.sisben.gov.co/dnp_sisbenconsulta'
        
        # Usar 'load' para carga inicial más rápida
        page.goto(page_url, wait_until='load', timeout=20000)
        
        # Esperar formulario directamente
        page.wait_for_selector('select#TipoID', state='visible', timeout=10000)
        time.sleep(0.2)  # Mínima pausa
        
        # Seleccionar tipo de documento: Cédula de Ciudadanía (value="3")
        logger.info("✍️ Seleccionando tipo de documento: Cédula de Ciudadanía")
        page.select_option('select#TipoID', '3')
        time.sleep(0.2)
        
        # Ingresar documento
        logger.info(f"✍️ Ingresando documento: {cedula_str}")
        page.fill('input#documento', cedula_str)
        time.sleep(0.2)
        
        # INTENTAR PRIMERO SIN CAPTCHA (como el código JS)
        logger.info("🚀 Intentando envío directo sin CAPTCHA...")
        try:
            # Hacer click directo sin resolver CAPTCHA primero
            page.click('input#botonenvio')
            time.sleep(2)  # Esperar como en el código JS
            
            # Verificar si hay resultados usando el selector del código JS
            card_resultado = page.query_selector('body > div.container > main > div > div.card.border.border-0 > div:nth-child(4)')
            if card_resultado:
                logger.info("✅ Resultados encontrados sin CAPTCHA!")
                # Continuar con extracción
            else:
                # Verificar si hay elementos de datos con selectores alternativos
                elementos_datos = page.query_selector_all('p.campo1.pt-1.pl-2.font-weight-bold, div.row.campo, p.etiqueta1')
                if elementos_datos and len(elementos_datos) > 0:
                    logger.info("✅ Elementos de datos encontrados sin CAPTCHA!")
                else:
                    raise Exception("No se encontraron resultados, intentando con CAPTCHA...")
        except Exception as e:
            logger.info(f"⚠️ Envío directo falló: {e}")
            logger.info("🤖 Resolviendo CAPTCHA como fallback...")
            
            # FALLBACK: Resolver CAPTCHA si el envío directo falló
            site_key = '6Lfh6kwcAAAAANT-kyprjG-m2yGmDmfOCvXinRE6'
            action = 'submit'
            
            captcha_token = solve_recaptcha(site_key, page_url, action=action)
            
            if not captcha_token:
                logger.error("❌ No se pudo resolver el CAPTCHA de SISBEN")
                return None
            
            # Inyectar token del captcha
            logger.info("💉 Inyectando token de CAPTCHA v3...")
            page.evaluate(f"""
                (function() {{
                    var tokenInput = document.createElement('input');
                    tokenInput.type = 'hidden';
                    tokenInput.name = 'g-recaptcha-response';
                    tokenInput.value = '{captcha_token}';
                    var form = document.querySelector('form');
                    if (form) {{
                        var existing = form.querySelector('input[name="g-recaptcha-response"]');
                        if (existing) {{
                            existing.remove();
                        }}
                        form.appendChild(tokenInput);
                    }}
                }})();
            """)
            
            time.sleep(0.5)
            
            # Enviar formulario con CAPTCHA
            logger.info("📤 Enviando formulario SISBEN con CAPTCHA...")
            try:
                with page.expect_navigation(wait_until='load', timeout=20000):
                    page.click('input#botonenvio')
            except PlaywrightTimeoutError:
                logger.warning("⚠️ Timeout en navegación SISBEN, verificando contenido...")
            
            time.sleep(2)  # Esperar como en el código JS
        
        # Esperar por los elementos de datos usando selectores específicos del código JS
        try:
            # Usar selectores más específicos del código JS
            page.wait_for_selector('p.campo1.pt-1.pl-2.font-weight-bold, div.row.campo, p.etiqueta1', timeout=10000, state='visible')
            logger.info("✅ Elementos de datos detectados en la página")
        except Exception as e:
            logger.warning(f"⚠️ No se encontraron elementos de datos esperados: {e}")
            time.sleep(1)
        
        # Extraer datos de la respuesta
        html = page.content()
        texto = page.inner_text('body')
        
        # Log adicional para debugging
        logger.info(f"📊 Longitud del HTML: {len(html)} caracteres")
        logger.info(f"📊 Longitud del texto extraído: {len(texto)} caracteres")
        
        # Si el HTML está vacío o muy corto, puede ser que la página se haya recargado
        if len(html) < 100:
            logger.error(f"❌ HTML muy corto ({len(html)} caracteres). Posible redirección o error.")
            logger.info(f"📍 URL final: {page.url}")
            logger.info(f"📝 HTML completo: {html}")
            
            # Intentar recargar la página o esperar más
            logger.info("🔄 Intentando esperar más tiempo...")
            time.sleep(5)
            html = page.content()
            texto = page.inner_text('body')
            logger.info(f"📊 HTML después de esperar: {len(html)} caracteres")
        
        if len(texto) > 0:
            logger.info(f"📝 Primeros 300 caracteres del texto: {texto[:300]}")
        else:
            logger.warning("⚠️ El texto extraído está vacío")
        
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
            
            # Método 2: Usar selectores específicos del código JS (más directo y rápido)
            if not any(campos.values()):
                logger.info("🔍 Intentando con selectores específicos del código JS...")
                try:
                    # Usar los selectores exactos del código JS
                    campos_js = page.query_selector_all('p.campo1.pt-1.pl-2.font-weight-bold')
                    etiquetas_js = page.query_selector_all('p.etiqueta1')
                    
                    logger.info(f"📊 Encontrados {len(campos_js)} campos y {len(etiquetas_js)} etiquetas")
                    
                    # Función para limpiar texto (como en el código JS)
                    def limpiar_texto(text):
                        if not text:
                            return ''
                        import re
                        return re.sub(r'\s+', ' ', str(text).strip().replace('\n', ' '))
                    
                    # Extraer información por etiquetas y valores (método del código JS)
                    for i, etiqueta_elem in enumerate(etiquetas_js):
                        if i < len(campos_js):
                            etiqueta_text = limpiar_texto(etiqueta_elem.inner_text())
                            valor = limpiar_texto(campos_js[i].inner_text())
                            
                            if not etiqueta_text or not valor:
                                continue
                            
                            etiqueta_lower = etiqueta_text.lower()
                            
                            # Mapear etiquetas a campos (como en el código JS)
                            if 'nombre' in etiqueta_lower and 'primer nombre' not in etiqueta_lower:
                                resultado_sisben['nombres'] = valor
                                logger.info(f"✓ Nombres extraídos (JS method): {valor}")
                            elif 'apellido' in etiqueta_lower:
                                resultado_sisben['apellidos'] = valor
                                logger.info(f"✓ Apellidos extraídos (JS method): {valor}")
                            elif ('documento' in etiqueta_lower or 'cédula' in etiqueta_lower or 'cedula' in etiqueta_lower) and 'tipo' not in etiqueta_lower:
                                resultado_sisben['numero_documento'] = valor
                                logger.info(f"✓ Número de documento extraído (JS method): {valor}")
                            elif 'tipo' in etiqueta_lower and 'documento' in etiqueta_lower:
                                resultado_sisben['tipo_documento'] = valor
                                logger.info(f"✓ Tipo de documento extraído (JS method): {valor}")
                            elif 'departamento' in etiqueta_lower:
                                resultado_sisben['departamento'] = valor
                                logger.info(f"✓ Departamento extraído (JS method): {valor}")
                            elif 'municipio' in etiqueta_lower:
                                resultado_sisben['municipio'] = valor
                                logger.info(f"✓ Municipio extraído (JS method): {valor}")
                    
                    # Si aún no hay datos, intentar método alternativo con divs
                    if not any(resultado_sisben.values()):
                        logger.info("🔍 Intentando método alternativo con divs...")
                        rows = page.query_selector_all('div.row.campo')
                        logger.info(f"📊 Encontrados {len(rows)} divs con clase 'row campo'")
                        
                        for row in rows:
                            etiqueta = row.query_selector('p.etiqueta1')
                            if etiqueta:
                                etiqueta_text = limpiar_texto(etiqueta.inner_text())
                                campo_elem = row.query_selector('p.campo1')
                                if campo_elem:
                                    valor = limpiar_texto(campo_elem.inner_text())
                                    
                                    if 'Nombres' in etiqueta_text:
                                        resultado_sisben['nombres'] = valor
                                    elif 'Apellidos' in etiqueta_text:
                                        resultado_sisben['apellidos'] = valor
                                    elif 'Tipo de documento' in etiqueta_text:
                                        resultado_sisben['tipo_documento'] = valor
                                    elif 'Número de documento' in etiqueta_text:
                                        resultado_sisben['numero_documento'] = valor
                                    elif 'Municipio' in etiqueta_text:
                                        resultado_sisben['municipio'] = valor
                                    elif 'Departamento' in etiqueta_text:
                                        resultado_sisben['departamento'] = valor
                except Exception as e:
                    logger.warning(f"⚠️ Error usando método JS para extraer datos: {e}")
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

def query_registraduria(page, cedula_str):
    """Consulta Registraduría y extrae datos del lugar de votación"""
    try:
        logger.info("🌐 Consultando Registraduría...")
        page_url = 'https://wsp.registraduria.gov.co/censo/consultar'
        
        # Usar 'load' para carga más rápida
        page.goto(page_url, wait_until='load', timeout=20000)
        
        # Esperar formulario directamente
        page.wait_for_selector('input#nuip', state='visible', timeout=10000)
        time.sleep(0.3)  # Reducido
        
        # Llenar cédula
        logger.info(f"✍️ Ingresando cédula: {cedula_str}")
        page.type('input#nuip', cedula_str, delay=random.randint(50, 100))  # Reducido delay
        
        time.sleep(0.3)  # Reducido
        
        # Seleccionar tipo de elección - debe ser "LUGAR DE VOTACIÓN ACTUAL..." (value="-1")
        try:
            page.wait_for_selector('select#tipo', state='visible', timeout=5000)
            page.select_option('select#tipo', '-1')
            logger.info("✓ Elección seleccionada: LUGAR DE VOTACIÓN ACTUAL...")
        except Exception as e:
            logger.warning(f"Error seleccionando elección: {e}")
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
        
        time.sleep(0.3)  # Reducido
        
        # Resolver reCAPTCHA
        site_key = '6LcthjAgAAAAAFIQLxy52074zanHv47cIvmIHglH'
        
        logger.info("🤖 Resolviendo reCAPTCHA v2 para Registraduría...")
        captcha_token = solve_recaptcha(site_key, page_url)
        
        if not captcha_token:
            logger.error("❌ No se pudo resolver el CAPTCHA de Registraduría")
            return None
        
        # Inyectar token del captcha
        logger.info("💉 Inyectando token de CAPTCHA...")
        page.evaluate(f"""
            var textarea = document.getElementById('g-recaptcha-response');
            if (textarea) {{
                textarea.innerHTML = '{captcha_token}';
                textarea.value = '{captcha_token}';
            }}
        """)
        
        time.sleep(0.5)  # Reducido
        
        # Enviar formulario
        logger.info("📤 Enviando formulario Registraduría...")
        try:
            # Usar 'load' para navegación más rápida
            with page.expect_navigation(wait_until='load', timeout=20000):
                page.click('input[type="submit"]#enviar')
        except PlaywrightTimeoutError:
            logger.warning("⚠️ Timeout en navegación Registraduría, verificando contenido...")
        
        # Esperar directamente por la tabla de resultados
        try:
            page.wait_for_selector('table', timeout=15000, state='visible')
            logger.info("✅ Tabla de resultados detectada")
        except:
            time.sleep(1)  # Fallback mínimo
        
        # Obtener contenido
        texto = page.inner_text('body')
        html = page.content()
        
        logger.info("📄 Contenido obtenido, parseando datos...")
        
        # Detectar errores
        texto_lower = texto.lower()
        if 'no se encontro' in texto_lower or 'no existe' in texto_lower or 'no se encontró' in texto_lower:
            logger.info("📝 Cédula no encontrada en Registraduría")
            return {"status": "not_found"}
        
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
            
            tabla = page.query_selector('table')
            if tabla:
                logger.info("📊 Tabla encontrada, extrayendo datos...")
                filas = tabla.query_selector_all('tr')
                
                headers = []
                data_row_index = -1
                
                for idx, fila in enumerate(filas):
                    celdas = fila.query_selector_all('td, th')
                    if len(celdas) >= 6:
                        primera_celda = celdas[0].inner_text().strip().upper()
                        if 'NUIP' in primera_celda or 'DEPARTAMENTO' in primera_celda:
                            headers = [celda.inner_text().strip() for celda in celdas]
                            logger.info(f"✓ Headers encontrados: {headers}")
                            if idx + 1 < len(filas):
                                data_row_index = idx + 1
                            break
                
                if headers and data_row_index >= 0 and data_row_index < len(filas):
                    fila_datos = filas[data_row_index]
                    celdas_datos = fila_datos.query_selector_all('td, th')
                    
                    if len(celdas_datos) >= len(headers):
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
            
            # Método 1.5: Parsear desde texto
            if not any([resultado['nuip'], resultado['departamento'], resultado['municipio']]):
                logger.info("🔍 Intentando parsear desde texto plano...")
                lineas = texto.split('\n')
                for i, linea in enumerate(lineas):
                    linea_clean = linea.strip()
                    if 'INFORMACIÓN DEL LUGAR DE VOTACIÓN' in linea_clean.upper():
                        if i + 2 < len(lineas):
                            linea_datos = lineas[i + 2].strip()
                            partes = linea_datos.split('\t') if '\t' in linea_datos else linea_datos.split()
                            if len(partes) < 6:
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
            
            # Asegurar que tenga al menos el NUIP
            if not resultado['nuip']:
                resultado['nuip'] = cedula_str
            
            # Verificar si se encontraron datos
            if any(resultado.values()):
                logger.info(f"✅ Datos Registraduría extraídos: {resultado}")
                return resultado
            else:
                logger.warning("⚠️ No se encontraron datos en Registraduría")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error parseando datos Registraduría: {e}", exc_info=True)
            return None
            
    except Exception as e:
        logger.error(f"❌ Error consultando Registraduría: {e}", exc_info=True)
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

def process_sisben_job(job_id, cedula_str):
    """Procesa SOLO consulta SISBEN"""
    context = None
    page = None
    browser = None
    playwright = None
    
    try:
        with jobs_lock:
            jobs[job_id]['status'] = 'querying_sisben'
            jobs[job_id]['message'] = 'Consultando SISBEN...'
            jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info(f"📋 Procesando job SISBEN {job_id}: {cedula_str}")
        
        browser, playwright = init_browser()
        user_agent = get_random_user_agent()
        logger.info(f"🌐 Usando User-Agent: {user_agent[:50]}...")
        
        context = browser.new_context(
            user_agent=user_agent,
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
        
        resultado_sisben = query_sisben(page, cedula_str)
        
        # Guardar resultado
        with jobs_lock:
            jobs[job_id]['status'] = 'completed'
            jobs[job_id]['result'] = {
                "status": "success",
                "datos": {
                    "sisben": resultado_sisben if resultado_sisben else None,
                    "registraduria": None
                }
            }
            jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info(f"✅ Job SISBEN {job_id} completado")
        
    except Exception as e:
        logger.error(f"❌ Error en job SISBEN {job_id}: {e}", exc_info=True)
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
            
def process_registraduria_job(job_id, cedula_str):
    """Procesa SOLO consulta Registraduría"""
    context = None
    page = None
    browser = None
    playwright = None
    
    try:
        with jobs_lock:
            jobs[job_id]['status'] = 'processing'
            jobs[job_id]['message'] = 'Consultando Registraduría...'
            jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info(f"📋 Procesando job Registraduría {job_id}: {cedula_str}")
        
        browser, playwright = init_browser()
        user_agent = get_random_user_agent()
        logger.info(f"🌐 Usando User-Agent: {user_agent[:50]}...")
        
        context = browser.new_context(
            user_agent=user_agent,
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
        
        resultado_registraduria = query_registraduria(page, cedula_str)
        
        # Guardar HTML para debugging
        try:
            html = page.content()
            with jobs_lock:
                html_responses[job_id] = html
        except:
            pass
        
        # Guardar resultado
        with jobs_lock:
            jobs[job_id]['status'] = 'completed'
            jobs[job_id]['result'] = {
                "status": "success",
                "datos": {
                    "sisben": None,
                    "registraduria": resultado_registraduria if resultado_registraduria else None
                }
            }
            jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info(f"✅ Job Registraduría {job_id} completado")
        
    except Exception as e:
        logger.error(f"❌ Error en job Registraduría {job_id}: {e}", exc_info=True)
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

def process_cedula_job(job_id, cedula_str, registraduria_mode='immediate'):
    """
    Procesa una consulta de cédula en modo híbrido:
    - SISBEN se ejecuta primero y se devuelve inmediatamente
    - Registraduría puede ejecutarse inmediatamente, diferida o omitirse
    
    Args:
        job_id: ID del job principal
        cedula_str: Número de cédula
        registraduria_mode: 'immediate' (inmediato), 'deferred' (diferido), 'skip' (omitir)
    """
    context = None
    page = None
    browser = None
    playwright = None
    
    try:
        # Actualizar estado
        with jobs_lock:
            jobs[job_id]['status'] = 'querying_sisben'
            jobs[job_id]['message'] = 'Consultando SISBEN...'
            jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info(f"📋 Procesando job {job_id}: {cedula_str} (modo Registraduría: {registraduria_mode})")
        
        # PRIMERO: Consultar SISBEN (siempre se ejecuta primero)
        browser, playwright = init_browser()
        user_agent = get_random_user_agent()
        logger.info(f"🌐 Usando User-Agent: {user_agent[:50]}...")
        
        context = browser.new_context(
            user_agent=user_agent,
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
        
        logger.info("🔄 Iniciando consulta SISBEN (primera consulta - rápida)...")
        resultado_sisben = query_sisben(page, cedula_str)
        
        # Cerrar browser de SISBEN para liberar recursos
        try:
            page.close()
            context.close()
            browser.close()
            playwright.stop()
            page = None
            context = None
            browser = None
            playwright = None
        except:
            pass
        
        # Guardar resultado parcial de SISBEN para mostrar inmediatamente
        with jobs_lock:
            if resultado_sisben:
                jobs[job_id]['result'] = {
                    "status": "partial",
                    "mensaje": "Consulta SISBEN completada. Datos disponibles inmediatamente.",
                    "datos": {
                        "sisben": resultado_sisben,
                        "registraduria": None
                    }
                }
                jobs[job_id]['status'] = 'sisben_completed'
                jobs[job_id]['updated_at'] = datetime.now()
            else:
                jobs[job_id]['result'] = {
                    "status": "partial",
                    "mensaje": "Consulta SISBEN completada (sin datos encontrados).",
                    "datos": {
                        "sisben": None,
                        "registraduria": None
                    }
                }
                jobs[job_id]['status'] = 'sisben_completed'
                jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info("✅ Resultado SISBEN guardado y disponible")
        
        # SEGUNDO: Manejar Registraduría según el modo
        if registraduria_mode == 'skip':
            # Solo SISBEN, marcar como completado
            with jobs_lock:
                jobs[job_id]['status'] = 'completed'
                jobs[job_id]['result'] = {
                    "status": "success",
                    "mensaje": "Consulta SISBEN completada (Registraduría omitida).",
                    "datos": {
                        "sisben": resultado_sisben,
                        "registraduria": None
                    }
                }
                jobs[job_id]['updated_at'] = datetime.now()
            logger.info(f"✅ Job {job_id} completado (solo SISBEN)")
            return
        
        elif registraduria_mode == 'deferred':
            # Programar Registraduría para después en un job separado
            logger.info("📅 Programando consulta Registraduría para ejecutarse después...")
            registraduria_job_id = str(uuid.uuid4())
            
            with jobs_lock:
                jobs[registraduria_job_id] = {
                    'cedula': cedula_str,
                    'status': 'pending',
                    'result': None,
                    'tipo': 'registraduria_deferred',
                    'parent_job_id': job_id,
                    'created_at': datetime.now(),
                    'updated_at': datetime.now(),
                }
                jobs[job_id]['registraduria_job_id'] = registraduria_job_id
                jobs[job_id]['message'] = 'SISBEN completado. Registraduría programada para procesar después.'
            
            # Programar Registraduría para ejecutarse después (con un pequeño delay)
            def delayed_registraduria():
                time.sleep(2)  # Pequeño delay para no saturar
                executor.submit(process_registraduria_job, registraduria_job_id, cedula_str)
            
            threading.Thread(target=delayed_registraduria, daemon=True).start()
            logger.info(f"📅 Job Registraduría {registraduria_job_id} programado para ejecutarse después")
            
            # Actualizar job principal para indicar que Registraduría está pendiente
            with jobs_lock:
                jobs[job_id]['status'] = 'sisben_completed'
                jobs[job_id]['result'] = {
                    "status": "partial",
                    "mensaje": "SISBEN completado. Registraduría en proceso en background...",
                    "datos": {
                        "sisben": resultado_sisben,
                        "registraduria": None,
                        "registraduria_job_id": registraduria_job_id
                    }
                }
                jobs[job_id]['updated_at'] = datetime.now()
            
            # Función para actualizar el job principal cuando Registraduría termine
            def update_parent_when_done():
                max_attempts = 60  # Máximo 5 minutos esperando
                attempts = 0
                while attempts < max_attempts:
                    time.sleep(5)
                    attempts += 1
                    with jobs_lock:
                        reg_job = jobs.get(registraduria_job_id)
                        if reg_job and reg_job['status'] in ['completed', 'error', 'not_found']:
                            # Extraer datos de Registraduría del resultado
                            resultado_reg = None
                            if reg_job.get('result') and reg_job['result'].get('datos'):
                                resultado_reg = reg_job['result']['datos'].get('registraduria')
                            
                            jobs[job_id]['status'] = 'completed'
                            jobs[job_id]['result'] = {
                                "status": "success",
                                "mensaje": "Consulta completa finalizada.",
                                "datos": {
                                    "sisben": resultado_sisben,
                                    "registraduria": resultado_reg
                                }
                            }
                            jobs[job_id]['updated_at'] = datetime.now()
                            logger.info(f"✅ Job principal {job_id} actualizado con datos de Registraduría")
                            break
                    if attempts >= max_attempts:
                        logger.warning(f"⏱️ Timeout esperando Registraduría para job {job_id}")
                        with jobs_lock:
                            jobs[job_id]['result'] = {
                                "status": "partial",
                                "mensaje": "SISBEN completado. Registraduría aún procesándose...",
                                "datos": {
                                    "sisben": resultado_sisben,
                                    "registraduria": None,
                                    "registraduria_job_id": registraduria_job_id
                                }
                            }
                            jobs[job_id]['updated_at'] = datetime.now()
                        break
            
            threading.Thread(target=update_parent_when_done, daemon=True).start()
            return
        
        else:  # registraduria_mode == 'immediate'
            # Ejecutar Registraduría inmediatamente después de SISBEN
            logger.info("🔄 Iniciando consulta Registraduría (segunda consulta - inmediata)...")
            
            # Crear nuevo browser para Registraduría
            browser, playwright = init_browser()
            user_agent = get_random_user_agent()
            
            context = browser.new_context(
                user_agent=user_agent,
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
            
            with jobs_lock:
                jobs[job_id]['status'] = 'processing'
                jobs[job_id]['message'] = 'Consultando Registraduría...'
                jobs[job_id]['updated_at'] = datetime.now()
            
            resultado_registraduria = query_registraduria(page, cedula_str)
            
            # Guardar HTML para debugging
            try:
                html = page.content()
                with jobs_lock:
                    html_responses[job_id] = html
            except:
                pass
            
            # Combinar resultados
            resultado_combinado = {
                "sisben": resultado_sisben if resultado_sisben else None,
                "registraduria": resultado_registraduria if resultado_registraduria else None
            }
            
            # Guardar resultado completo
            with jobs_lock:
                jobs[job_id]['status'] = 'completed'
                jobs[job_id]['result'] = {
                    "status": "success",
                    "mensaje": "Consulta completa finalizada.",
                    "datos": resultado_combinado
                }
                jobs[job_id]['updated_at'] = datetime.now()
            
            logger.info(f"✅ Job {job_id} completado exitosamente (ambos servicios)")
            logger.info(f"📊 Datos SISBEN: {resultado_sisben}")
            logger.info(f"📊 Datos Registraduría: {resultado_registraduria}")
        
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
    trabajos_activos = sum(1 for j in jobs.values() if j['status'] in ['pending', 'processing', 'solving_captcha', 'querying_sisben'])
    return jsonify({
        "status": "healthy",
        "jobs_totales": len(jobs),
        "jobs_activos": trabajos_activos,
        "max_workers": MAX_WORKERS,
        "capacidad_disponible": MAX_WORKERS - trabajos_activos,
        "user_agents_cargados": len(USER_AGENTS),
        "captcha_configured": bool(TWOCAPTCHA_API_KEY),
        "api_key_preview": TWOCAPTCHA_API_KEY[:8] + "..." if TWOCAPTCHA_API_KEY else None,
        "timestamp": time.time()
    }), 200

@app.route('/consulta_sisben', methods=['POST'])
def consulta_sisben_async():
    """Crea un job asíncrono SOLO para consultar SISBEN"""
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
        
        job_id = str(uuid.uuid4())
        
        with jobs_lock:
            jobs[job_id] = {
                'cedula': cedula_str,
                'status': 'pending',
                'result': None,
                'tipo': 'sisben_only',
                'created_at': datetime.now(),
                'updated_at': datetime.now(),
            }
        
        executor.submit(process_sisben_job, job_id, cedula_str)
        
        trabajos_activos = sum(1 for j in jobs.values() if j['status'] in ['pending', 'processing', 'solving_captcha', 'querying_sisben'])
        logger.info(f"🆕 Job SISBEN {job_id} creado para {cedula_str} (Trabajos activos: {trabajos_activos}/{MAX_WORKERS})")
        
        return jsonify({
            "status": "accepted",
            "job_id": job_id,
            "cedula": cedula_str,
            "tipo": "sisben_only",
            "mensaje": "Consulta SISBEN iniciada. El proceso tomará 30-60 segundos.",
            "max_concurrent": MAX_WORKERS,
            "trabajos_activos": trabajos_activos,
            "endpoints": {
                "status": f"/job/{job_id}",
                "result": f"/job/{job_id}/result"
            }
        }), 202
        
    except Exception as e:
        logger.error(f"Error creando job SISBEN: {e}")
        return jsonify({"status": "error", "mensaje": str(e)}), 500

@app.route('/consulta_registraduria', methods=['POST'])
def consulta_registraduria_async():
    """Crea un job asíncrono SOLO para consultar Registraduría"""
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
        
        job_id = str(uuid.uuid4())
        
        with jobs_lock:
            jobs[job_id] = {
                'cedula': cedula_str,
                'status': 'pending',
                'result': None,
                'tipo': 'registraduria_only',
                'created_at': datetime.now(),
                'updated_at': datetime.now(),
            }
        
        executor.submit(process_registraduria_job, job_id, cedula_str)
        
        trabajos_activos = sum(1 for j in jobs.values() if j['status'] in ['pending', 'processing', 'solving_captcha', 'querying_sisben'])
        logger.info(f"🆕 Job Registraduría {job_id} creado para {cedula_str} (Trabajos activos: {trabajos_activos}/{MAX_WORKERS})")
        
        return jsonify({
            "status": "accepted",
            "job_id": job_id,
            "cedula": cedula_str,
            "tipo": "registraduria_only",
            "mensaje": "Consulta Registraduría iniciada. El proceso tomará 30-60 segundos.",
            "max_concurrent": MAX_WORKERS,
            "trabajos_activos": trabajos_activos,
            "endpoints": {
                "status": f"/job/{job_id}",
                "result": f"/job/{job_id}/result"
            }
        }), 202
        
    except Exception as e:
        logger.error(f"Error creando job Registraduría: {e}")
        return jsonify({"status": "error", "mensaje": str(e)}), 500

@app.route('/consulta_cedula', methods=['POST'])
def consulta_cedula_async():
    """
    Crea un job asíncrono para consultar AMBOS (SISBEN primero, luego Registraduría)
    Modo híbrido: SISBEN se entrega primero (rápido), Registraduría puede ejecutarse inmediatamente o diferida
    
    Parámetros:
    - cedula: Número de cédula (requerido)
    - registraduria_mode: 'immediate' (inmediato, por defecto), 'deferred' (diferido), 'skip' (omitir)
    """
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
        
        # Obtener modo de Registraduría (por defecto: immediate)
        registraduria_mode = data.get('registraduria_mode', 'immediate')
        if registraduria_mode not in ['immediate', 'deferred', 'skip']:
            registraduria_mode = 'immediate'
        
        # Crear job
        job_id = str(uuid.uuid4())
        
        with jobs_lock:
            jobs[job_id] = {
                'cedula': cedula_str,
                'status': 'pending',
                'result': None,
                'tipo': 'ambos',
                'registraduria_mode': registraduria_mode,
                'created_at': datetime.now(),
                'updated_at': datetime.now(),
            }
        
        # Enviar a procesar usando ThreadPoolExecutor
        executor.submit(process_cedula_job, job_id, cedula_str, registraduria_mode)
        
        # Contar trabajos activos
        trabajos_activos = sum(1 for j in jobs.values() if j['status'] in ['pending', 'processing', 'solving_captcha', 'querying_sisben', 'sisben_completed'])
        
        # Mensaje según el modo
        mensajes = {
            'immediate': "Consulta iniciada. SISBEN primero (30-60s, disponible inmediatamente), luego Registraduría inmediata (30-60s). Total: 60-120 segundos.",
            'deferred': "Consulta iniciada. SISBEN primero (30-60s, disponible inmediatamente), Registraduría se procesará después en background (~60s).",
            'skip': "Consulta iniciada. Solo SISBEN (30-60s, disponible inmediatamente). Registraduría omitida."
        }
        
        logger.info(f"🆕 Job AMBOS {job_id} creado para {cedula_str} (modo: {registraduria_mode}, Trabajos activos: {trabajos_activos}/{MAX_WORKERS})")
        
        # Retornar inmediatamente
        return jsonify({
            "status": "accepted",
            "job_id": job_id,
            "cedula": cedula_str,
            "tipo": "ambos",
            "registraduria_mode": registraduria_mode,
            "mensaje": mensajes[registraduria_mode],
            "max_concurrent": MAX_WORKERS,
            "trabajos_activos": trabajos_activos,
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
    
    # Si hay resultado parcial (SISBEN completado), retornarlo
    result = job.get('result')
    if result and (result.get('status') == 'partial' or status == 'sisben_completed'):
        return jsonify(result), 200
    
    if status in ['pending', 'processing', 'solving_captcha', 'querying_sisben', 'sisben_completed']:
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
    trabajos_activos = sum(1 for j in jobs.values() if j['status'] in ['pending', 'processing', 'solving_captcha', 'querying_sisben'])
    return jsonify({
        "servicio": "Registraduria API - 2Captcha",
        "version": "8.0",
        "caracteristicas": {
            "multitarea": True,
            "max_consultas_simultaneas": MAX_WORKERS,
            "user_agents_aleatorios": len(USER_AGENTS),
            "resultados_parciales": True
        },
        "captcha_service": "2Captcha",
        "captcha_configured": bool(TWOCAPTCHA_API_KEY),
        "interfaz_web": "GET /consultar",
        "estado_actual": {
            "trabajos_activos": trabajos_activos,
            "trabajos_completados": sum(1 for j in jobs.values() if j['status'] == 'completed'),
            "max_workers": MAX_WORKERS
        },
        "endpoints": {
            "consulta_completa": "POST /consulta_cedula - Consulta AMBOS (SISBEN primero, luego Registraduría)",
            "consulta_sisben": "POST /consulta_sisben - Consulta SOLO SISBEN (datos personales)",
            "consulta_registraduria": "POST /consulta_registraduria - Consulta SOLO Registraduría (mesa de votación)",
            "estado_job": "GET /job/{job_id}",
            "resultado_job": "GET /job/{job_id}/result",
            "health": "GET /health",
            "lovable_worker": {
                "start": "POST /lovable/worker/start - Inicia worker de Lovable Cloud",
                "stop": "POST /lovable/worker/stop - Detiene worker de Lovable Cloud",
                "status": "GET /lovable/worker/status - Estado del worker"
            }
        },
        "modo_hibrido": {
            "descripcion": "Sistema híbrido: SISBEN se entrega primero (rápido), Registraduría puede ejecutarse inmediatamente o diferida",
            "modos_registraduria": {
                "immediate": "Ejecuta Registraduría inmediatamente después de SISBEN (por defecto)",
                "deferred": "SISBEN primero, Registraduría se procesa después en background",
                "skip": "Solo SISBEN, omite Registraduría"
            }
        },
        "ejemplo_completo": {
            "paso_1": "POST /consulta_cedula con {cedula: '1087549965', registraduria_mode: 'immediate'}",
            "paso_2": "Guardar el job_id retornado",
            "paso_3": "GET /job/{job_id}/result - SISBEN disponible en 30-60s, Registraduría en 60-120s",
            "paso_4": "Los datos de SISBEN aparecen primero como resultado parcial"
        },
        "ejemplo_hibrido": {
            "sisben_rapido": "POST /consulta_cedula con {cedula: '1087549965', registraduria_mode: 'deferred'}",
            "descripcion": "SISBEN se entrega en 30-60s, Registraduría se procesa después en background (~60s)",
            "solo_sisben": "POST /consulta_cedula con {cedula: '1087549965', registraduria_mode: 'skip'}"
        },
        "ejemplo_independiente": {
            "sisben": "POST /consulta_sisben con {cedula: '1087549965'} - Solo datos personales (30-60s)",
            "registraduria": "POST /consulta_registraduria con {cedula: '1087549965'} - Solo mesa de votación (30-60s)"
        },
        "nota": "Sistema híbrido: SISBEN siempre se entrega primero (rápido). Registraduría puede ejecutarse inmediatamente, diferida o omitirse. Consulta completa usa 2 créditos de 2Captcha. Consultas independientes usan 1 crédito cada una."
    })

# Worker de Lovable Cloud (opcional)
lovable_worker = None
lovable_worker_thread = None

@app.route('/lovable/worker/start', methods=['POST'])
def start_lovable_worker():
    """Inicia el worker de Lovable Cloud en background"""
    global lovable_worker, lovable_worker_thread
    
    try:
        if lovable_worker and lovable_worker.running:
            return jsonify({
                "status": "already_running",
                "mensaje": "El worker ya está en ejecución"
            }), 200
        
        from consulta_service import iniciar_worker_en_background
        lovable_worker, lovable_worker_thread = iniciar_worker_en_background()
        
        return jsonify({
            "status": "started",
            "mensaje": "Worker de Lovable Cloud iniciado correctamente"
        }), 200
        
    except Exception as e:
        logger.error(f"Error iniciando worker: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "mensaje": str(e)
        }), 500

@app.route('/lovable/worker/stop', methods=['POST'])
def stop_lovable_worker():
    """Detiene el worker de Lovable Cloud"""
    global lovable_worker, lovable_worker_thread
    
    try:
        if lovable_worker:
            lovable_worker.running = False
            lovable_worker = None
            lovable_worker_thread = None
            return jsonify({
                "status": "stopped",
                "mensaje": "Worker detenido correctamente"
            }), 200
        else:
            return jsonify({
                "status": "not_running",
                "mensaje": "El worker no está en ejecución"
            }), 200
        
    except Exception as e:
        logger.error(f"Error deteniendo worker: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "mensaje": str(e)
        }), 500

@app.route('/lovable/worker/status', methods=['GET'])
def lovable_worker_status():
    """Obtiene el estado del worker de Lovable Cloud"""
    global lovable_worker, lovable_worker_thread
    
    is_running = lovable_worker is not None and lovable_worker.running
    thread_alive = lovable_worker_thread is not None and lovable_worker_thread.is_alive()
    
    return jsonify({
        "status": "running" if (is_running and thread_alive) else "stopped",
        "worker_active": is_running,
        "thread_alive": thread_alive
    }), 200

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=10000)

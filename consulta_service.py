"""
consulta_service.py
Servicio para consultar SISBEN y Registraduría e integrar con Lovable Cloud
Integra con las funciones de app.py
"""

import requests
import time
import logging
import threading
from typing import Optional, Dict, Any, List
from datetime import datetime
from playwright.sync_api import sync_playwright

# Importar funciones y configuraciones de app.py
import sys
import os

# Agregar el directorio actual al path para importar app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Importar funciones necesarias de app.py
# Nota: Importamos el módulo completo para evitar problemas circulares
import app

# Configuración
SUPABASE_FUNCTIONS_URL = "https://lsdnopjulddzkkboarsp.supabase.co/functions/v1"
CONSULTA_API_TOKEN = "FaidersAltamartokenelectoral123"  # El token que configuraste en Lovable

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ConsultaService:
    """Servicio para manejar consultas de SISBEN y Registraduría"""
    
    def __init__(self, token: str = CONSULTA_API_TOKEN):
        self.token = token
        self.base_url = SUPABASE_FUNCTIONS_URL
        self.running = False
    
    def obtener_consultas_pendientes(self, tipo: str = 'sisben', limit: int = 50) -> List[Dict]:
        """
        Obtiene cédulas pendientes de consultar.
        
        Args:
            tipo: 'sisben' para consultas nuevas, 'registraduria' para post-SISBEN
            limit: Máximo de registros a obtener
            
        Returns:
            Lista de consultas pendientes
        """
        try:
            url = f"{self.base_url}/consultas-pendientes"
            params = {
                'token': self.token,
                'tipo': tipo,
                'limit': limit
            }
            
            response = requests.get(url, params=params, timeout=30)
            
            if response.status_code == 401:
                logger.error("Token inválido o no autorizado")
                return []
            
            response.raise_for_status()
            data = response.json()
            
            logger.info(f"Obtenidas {len(data.get('consultas', []))} consultas tipo '{tipo}'. "
                       f"Total pendientes: {data.get('total_pendientes', 0)}")
            
            return data.get('consultas', [])
            
        except requests.RequestException as e:
            logger.error(f"Error obteniendo consultas pendientes: {e}")
            return []
    
    def enviar_resultado(
        self, 
        cola_id: str, 
        cedula: str, 
        tipo: str, 
        exito: bool,
        datos: Optional[Dict] = None,
        error: Optional[str] = None
    ) -> bool:
        """
        Envía el resultado de una consulta a Lovable Cloud.
        
        Args:
            cola_id: ID del registro en cola_consultas
            cedula: Número de cédula consultada
            tipo: 'sisben' o 'registraduria'
            exito: True si la consulta fue exitosa
            datos: Diccionario con los datos obtenidos
            error: Mensaje de error si exito=False
            
        Returns:
            True si se envió correctamente
        """
        try:
            # Intentar diferentes endpoints posibles
            endpoints = [
                f"{self.base_url}/recibir-datos",
                f"{self.base_url}/recibir_datos",
                f"{self.base_url}/actualizar-consulta",
                f"{self.base_url}/actualizar_consulta"
            ]
            
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'cola_id': cola_id,
                'cedula': cedula,
                'tipo': tipo,
                'exito': exito,
                'datos': datos if exito else None,
                'error': error if not exito else None
            }
            
            # Intentar con el primer endpoint
            url = endpoints[0]
            logger.info(f"📤 Enviando resultado a: {url}")
            
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            
            if response.status_code == 404:
                logger.warning(f"⚠️ Endpoint {url} no encontrado (404). Verifica la URL del endpoint en Lovable Cloud.")
                logger.info(f"💡 Endpoints sugeridos: {', '.join(endpoints)}")
                return False
            
            if response.status_code == 401:
                logger.error("❌ Token inválido o no autorizado")
                return False
            
            response.raise_for_status()
            result = response.json()
            
            if exito:
                logger.info(f"✅ Datos enviados exitosamente para cédula {cedula} ({tipo})")
            else:
                logger.warning(f"⚠️ Error registrado para cédula {cedula}: {error}")
            
            return result.get('success', False)
            
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ Error HTTP enviando resultado para {cedula}: {e}")
            if hasattr(e.response, 'status_code'):
                logger.error(f"   Status code: {e.response.status_code}")
                logger.error(f"   Response: {e.response.text[:200]}")
            return False
        except requests.RequestException as e:
            logger.error(f"❌ Error enviando resultado para {cedula}: {e}")
            return False


def consultar_sisben_real(cedula: str) -> Dict[str, Any]:
    """
    Consulta datos de SISBEN para una cédula usando las funciones de app.py.
    
    Returns:
        Dict con 'exito', 'datos' y opcionalmente 'error'
    """
    context = None
    page = None
    browser = None
    playwright = None
    
    try:
        logger.info(f"🌐 Iniciando consulta SISBEN para cédula: {cedula}")
        
        browser, playwright = app.init_browser()
        user_agent = app.get_random_user_agent()
        
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
        
        # Usar la función query_sisben de app.py
        resultado = app.query_sisben(page, cedula)
        
        if resultado and resultado.get('status') == 'not_found':
            return {
                'exito': False,
                'error': 'Cédula no encontrada en SISBEN'
            }
        
        if resultado and any(resultado.values()):
            # Mapear los datos al formato esperado por Lovable
            datos_mapeados = {
                'nombres': resultado.get('nombres'),
                'apellidos': resultado.get('apellidos'),
                'tipo_documento': resultado.get('tipo_documento'),
                'numero_documento': resultado.get('numero_documento'),
                'municipio': resultado.get('municipio'),
                'departamento': resultado.get('departamento')
            }
            
            return {
                'exito': True,
                'datos': datos_mapeados
            }
        else:
            return {
                'exito': False,
                'error': 'No se encontraron datos en SISBEN'
            }
            
    except Exception as e:
        logger.error(f"Error consultando SISBEN para {cedula}: {e}", exc_info=True)
        return {
            'exito': False,
            'error': str(e)
        }
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


def consultar_registraduria_real(cedula: str) -> Dict[str, Any]:
    """
    Consulta datos de Registraduría para una cédula usando las funciones de app.py.
    
    Returns:
        Dict con 'exito', 'datos' y opcionalmente 'error'
    """
    context = None
    page = None
    browser = None
    playwright = None
    
    try:
        logger.info(f"🗳️ Iniciando consulta Registraduría para cédula: {cedula}")
        
        browser, playwright = app.init_browser()
        user_agent = app.get_random_user_agent()
        
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
        
        # Usar la función query_registraduria de app.py
        resultado = app.query_registraduria(page, cedula)
        
        if resultado and resultado.get('status') == 'not_found':
            return {
                'exito': False,
                'error': 'Cédula no encontrada en Registraduría'
            }
        
        if resultado and any(resultado.values()):
            # Mapear los datos al formato esperado por Lovable
            datos_mapeados = {
                'nuip': resultado.get('nuip'),
                'departamento': resultado.get('departamento'),
                'municipio': resultado.get('municipio'),
                'puesto': resultado.get('puesto'),
                'direccion': resultado.get('direccion'),
                'mesa': resultado.get('mesa'),
                # Campos adicionales para compatibilidad
                'municipio_votacion': resultado.get('municipio'),
                'departamento_votacion': resultado.get('departamento'),
                'direccion_puesto': resultado.get('direccion'),
                'puesto_votacion': resultado.get('puesto')
            }
            
            return {
                'exito': True,
                'datos': datos_mapeados
            }
        else:
            return {
                'exito': False,
                'error': 'No se encontraron datos en Registraduría'
            }
            
    except Exception as e:
        logger.error(f"Error consultando Registraduría para {cedula}: {e}", exc_info=True)
        return {
            'exito': False,
            'error': str(e)
        }
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


def procesar_consultas(service: ConsultaService, max_iteraciones: Optional[int] = None):
    """
    Función principal que procesa consultas pendientes.
    Ejecuta en loop para polling continuo.
    
    Args:
        service: Instancia de ConsultaService
        max_iteraciones: Número máximo de iteraciones (None para infinito)
    """
    logger.info("🚀 Iniciando servicio de consultas...")
    service.running = True
    iteracion = 0
    
    while service.running:
        try:
            if max_iteraciones and iteracion >= max_iteraciones:
                logger.info(f"✅ Completadas {iteracion} iteraciones")
                break
            
            iteracion += 1
            logger.info(f"🔄 Iteración {iteracion} - Consultando pendientes...")
            
            # ========== FASE 1: Consultar SISBEN ==========
            consultas_sisben = service.obtener_consultas_pendientes(tipo='sisben', limit=10)
            
            for consulta in consultas_sisben:
                if not service.running:
                    break
                    
                cedula = consulta['cedula']
                cola_id = consulta['id']
                
                logger.info(f"📋 Consultando SISBEN para cédula: {cedula} (cola_id: {cola_id})")
                
                resultado = consultar_sisben_real(cedula)
                
                service.enviar_resultado(
                    cola_id=cola_id,
                    cedula=cedula,
                    tipo='sisben',
                    exito=resultado['exito'],
                    datos=resultado.get('datos'),
                    error=resultado.get('error')
                )
                
                # Pausa entre consultas para no saturar
                time.sleep(2)
            
            # ========== FASE 2: Consultar Registraduría ==========
            # (Para cédulas que ya tienen SISBEN completado)
            consultas_reg = service.obtener_consultas_pendientes(tipo='registraduria', limit=10)
            
            for consulta in consultas_reg:
                if not service.running:
                    break
                    
                cedula = consulta['cedula']
                cola_id = consulta['id']
                
                logger.info(f"🗳️ Consultando Registraduría para cédula: {cedula} (cola_id: {cola_id})")
                
                resultado = consultar_registraduria_real(cedula)
                
                service.enviar_resultado(
                    cola_id=cola_id,
                    cedula=cedula,
                    tipo='registraduria',
                    exito=resultado['exito'],
                    datos=resultado.get('datos'),
                    error=resultado.get('error')
                )
                
                time.sleep(2)
            
            # Si no hay consultas pendientes, esperar antes de revisar de nuevo
            if not consultas_sisben and not consultas_reg:
                logger.info("💤 Sin consultas pendientes. Esperando 30 segundos...")
                time.sleep(30)
            else:
                # Pequeña pausa entre ciclos
                time.sleep(5)
                
        except KeyboardInterrupt:
            logger.info("🛑 Servicio detenido por usuario")
            service.running = False
            break
        except Exception as e:
            logger.error(f"Error en ciclo principal: {e}", exc_info=True)
            time.sleep(10)  # Esperar antes de reintentar
    
    service.running = False
    logger.info("✅ Servicio de consultas finalizado")


def iniciar_worker_en_background():
    """Inicia el worker en un thread separado"""
    service = ConsultaService()
    thread = threading.Thread(target=procesar_consultas, args=(service,), daemon=True)
    thread.start()
    logger.info("🔄 Worker de consultas iniciado en background")
    return service, thread


# ============================================
# SCRIPT PARA EJECUTAR
# ============================================
if __name__ == "__main__":
    import signal
    
    service = ConsultaService()
    
    def signal_handler(sig, frame):
        logger.info("🛑 Recibida señal de interrupción, deteniendo servicio...")
        service.running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Ejecutar el polling continuo
        procesar_consultas(service)
    except Exception as e:
        logger.error(f"Error fatal: {e}", exc_info=True)
    finally:
        logger.info("👋 Servicio finalizado")


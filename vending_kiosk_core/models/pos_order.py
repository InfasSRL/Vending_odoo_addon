# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""
Extensión de pos.order para soporte de vending machines.

Agrega campos para tracking del flujo de vending:
- Referencias de transacción
- Estado del proceso de vending
- Datos del QR generado
- Timestamps de eventos
"""

import logging
from odoo import api, fields, models, _  # type: ignore
from odoo.exceptions import UserError  # type: ignore

_logger = logging.getLogger(__name__)

# Namespace para pg_advisory_xact_lock — int32 que identifica locks de webhook vending.
# Valor ASCII de "vend" (0x76656E64). Junto con order.id forma una clave única por orden.
_VENDING_WEBHOOK_LOCK_NS = 0x76656E64


class PosOrder(models.Model):
    """Extensión de POS Order para vending machines."""

    _inherit = 'pos.order'

    # Referencias
    vending_reference = fields.Char(
        string='Referencia para Expendedora',
        size=36,
        index=True,
        copy=False,
        help='Referencia única de la transacción de vending'
    )
    
    # Estado del proceso
    vending_status = fields.Selection([
        ('draft', 'Borrador'),
        ('qr_ready', 'QR listo'),
        ('qr_expired', 'QR vencido'),
        ('user_cancelled', 'Cancelado por usuario'),
        ('payment_error', 'Error de pago'),
        ('payment_success', 'Pago exitoso'),
        ('vending_delivery_error', 'Falla de entrega'),
        ('vending_delivery_success', 'Entrega confirmada'),
    ],
        string='Estado de Expendedora',
        default='draft',
        index=True,
        copy=False,
        help='Estados del flujo de vending desde la creación hasta la entrega'
    )
    
    # Relaciones con vending
    vending_machine_id = fields.Many2one(
        'vending.machine',
        string='Máquina Expendedora',
        index=True,
    )
    vending_slot_id = fields.Many2one(
        'vending.slot',
        string='Slot de Máquina Expendedora',
        index=True,
    )

    vending_webhook_received_at = fields.Datetime(
        string='Momento de Recepción del Webhook',
        copy=False,
        help='Momento en que se recibió el webhook de confirmación'
    )
    
    vending_qr_created_at = fields.Datetime(
        string='Momento de Creación del QR',
        copy=False,
        help='Momento en que se generó el QR de pago'
    )
    
    vending_error_description = fields.Text(
        string='Descripción del Error',
        copy=False,
        help='Descripción del error si el webhook reportó un error'
    )
    
    vending_internal_error = fields.Text(
        string='Error Interno de Odoo',
        copy=False,
        help='Error que ocurrió internamente en Odoo durante el procesamiento (no es un error real del vending)'
    )
    
    vending_delivery_id = fields.Many2one(
        'stock.picking',
        string='Albarán de Entrega',
        copy=False,
        help='Albarán de stock creado para la entrega de los productos',
        readonly=True,
    )
    
    def _check_webhook_duplicate(self):
        """
        Verifica si este webhook ya fue procesado antes.

        Una orden se considera ya procesada si su estado está en uno de:
        - vending_delivery_success: Entrega confirmada
        - vending_delivery_error: Error de entrega
        - payment_error: Error de pago
        - qr_expired: QR vencido
        - user_cancelled: Cancelada por usuario

        Defensa adicional: si la orden ya tiene factura o pago registrado, también
        se considera duplicada (cubre el caso donde un intento previo alcanzó a
        crear pago/factura pero falló antes de marcar el vending_status terminal).

        Returns:
            bool: True si es un webhook duplicado, False si es nuevo
        """
        self.ensure_one()

        # Estados que indican que el webhook ya fue procesado
        processed_statuses = (
            'vending_delivery_success',
            'vending_delivery_error',
            'payment_error',
            'qr_expired',
            'user_cancelled',
        )

        if self.vending_status in processed_statuses:
            _logger.warning(
                f"Webhook duplicado detectado para order {self.vending_reference}. "
                f"Estado actual: {self.vending_status}"
            )
            return True

        if self.account_move or self.payment_ids:
            _logger.warning(
                f"Webhook duplicado detectado para order {self.vending_reference} por datos: "
                f"account_move={bool(self.account_move)}, payment_ids={len(self.payment_ids)}. "
                f"Estado actual: {self.vending_status}"
            )
            return True

        return False

    def _register_internal_error(self, error_name, error_description):
        """
        Registra un error interno de Odoo sin cambiar vending_status.
        Se usa cuando el webhook fue SUCCESS pero Odoo falló internamente.
        
        El vending_status se mantiene como está (generalmente 'vending_delivery_success')
        porque el producto YA se despachó de la máquina.
        
        Args:
            error_name: Nombre corto del error (ej: 'PAYMENT_METHOD_NOT_CONFIGURED')
            error_description: Descripción técnica detallada
        """
        self.ensure_one()
        
        error_message = f"{error_name}: {error_description}"
        
        # Guardar el error interno
        self.write({'vending_internal_error': error_message})
        
        # Agregar mensaje al chatter de la máquina para auditoria
        if self.vending_machine_id:
            self.vending_machine_id.message_post(
                body=_(
                    'Error interno procesando orden %(order_ref)s: %(error)s',
                    order_ref=self.vending_reference,
                    error=error_message
                ),
                message_type='notification',
                subtype_xmlid='mail.mt_note'
            )
        
        # Log del error
        _logger.warning(
            f"Error interno registrado para order {self.vending_reference}: {error_message}"
        )

    def _normalize_webhook_description(self, description):
        """
        Normaliza la descripción del webhook para logging interno.
        """
        if not description:
            return None
        if isinstance(description, dict):
            return str(description)
        return str(description)

    def _get_user_friendly_error_description(self, error_code):
        """
        Convierte códigos de error técnicos a descripciones amigables en español.
        
        Args:
            error_code (str): Código del error (ej: "DISPENSING_STUCK")
            
        Returns:
            str: Descripción amigable para mostrar al usuario
        """
        if not error_code:
            return "Ocurrió un error desconocido"
        
        error_code = str(error_code).upper().strip()
        
        # Mapeo de códigos a descripciones amigables
        friendly_descriptions = {
            # Errores de dispensación
            'DISPENSING_STUCK': 'Se atascó el producto en la máquina',
            'DISPENSING_NO_STOCK': 'No hay stock disponible del producto',
            'DISPENSING_MECHANICAL_FAILURE': 'La máquina experimentó un fallo mecánico',
            'DISPENSING_SENSOR_FAILURE': 'Un sensor de la máquina no está funcionando correctamente',
            'DISPENSING_TIMEOUT': 'La máquina tardó demasiado en despensar el producto',
            'DISPENSING_BLOCKED': 'La máquina está bloqueada y no puede despensar',
            
            # Errores de pago
            'PAYMENT_REJECTED': 'Tu pago fue rechazado',
            'PAYMENT_TIMEOUT': 'El pago expiró por tiempo agotado',
            'PAYMENT_INSUFFICIENT_FUNDS': 'Fondos insuficientes en tu cuenta',
            'PAYMENT_NETWORK_ERROR': 'Error de conexión al procesar el pago',
            'PAYMENT_CANCELLED': 'El pago fue cancelado',
            'PAYMENT_REFUNDED': 'Tu pago fue devuelto',
            'PAYMENT_INVALID_CARD': 'La tarjeta no es válida',
            'PAYMENT_EXPIRED_CARD': 'La tarjeta ha expirado',
            
            # Errores del sistema
            'SYSTEM_OFFLINE': 'La máquina está sin conexión',
            'SYSTEM_INTERNAL_ERROR': 'La máquina experimentó un error interno',
            'SYSTEM_NETWORK_ERROR': 'Problema de conexión con la máquina',
            'SYSTEM_MAINTENANCE': 'La máquina está en mantenimiento',
            
            # Códigos genéricos
            'ERROR_GENERIC': 'Ocurrió un error en el proceso',
            'ERROR': 'Ocurrió un error desconocido',
        }
        
        # Retornar descripción amigable o un mensaje genérico
        return friendly_descriptions.get(error_code, f"Ocurrió un error: {error_code}")

    def _get_error_type_label(self, error_type):
        """
        Convierte el tipo de error interno a una etiqueta amigable.
        
        Args:
            error_type (str): Tipo de error ('payment' o 'delivery')
            
        Returns:
            str: Etiqueta amigable (ej: "Error en el Pago" o "Error de Entrega")
        """
        type_labels = {
            'payment': 'Error en el Pago',
            'delivery': 'Error de Entrega',
            'internal': 'Error Interno',
        }
        return type_labels.get(error_type, 'Error')

    def _infer_error_type_from_description(self, description):
        """
        Infiere el tipo de error desde códigos estructurados de descripción.
        Mapea códigos Winfas a categorías internas.
        """
        if not description:
            return 'delivery'

        text = str(description).upper()
        
        # Mapeo de códigos Winfas a categorías
        payment_error_codes = [
            'PAYMENT_REJECTED',
            'PAYMENT_TIMEOUT', 
            'PAYMENT_INSUFFICIENT_FUNDS',
            'PAYMENT_NETWORK_ERROR',
            'PAYMENT_CANCELLED',
            'PAYMENT_INVALID_CARD',
            'PAYMENT_EXPIRED_CARD'
        ]
        
        dispensing_error_codes = [
            'DISPENSING_STUCK',
            'DISPENSING_NO_STOCK',
            'DISPENSING_MECHANICAL_FAILURE', 
            'DISPENSING_SENSOR_FAILURE',
            'DISPENSING_TIMEOUT',
            'DISPENSING_BLOCKED'
        ]
        
        system_error_codes = [
            'SYSTEM_OFFLINE',
            'SYSTEM_INTERNAL_ERROR',
            'SYSTEM_NETWORK_ERROR',
            'SYSTEM_MAINTENANCE'
        ]
        
        # Verificar códigos de pago
        for code in payment_error_codes:
            if code in text:
                return 'payment'
        
        # Verificar códigos de dispensación y sistema (ambos van a delivery error)
        for code in dispensing_error_codes + system_error_codes:
            if code in text:
                return 'delivery'
        
        # Fallback: análisis de palabras clave para retrocompatibilidad
        payment_keywords = [
            'PAGO', 'PAYMENT', 'TARJETA', 'CREDITO', 'CRÉDITO', 'DEBITO', 'DÉBITO',
            'DINERO EN CUENTA', 'CUENTA', 'SALDO', 'RECHAZADO', 'RECHAZO',
        ]
        for keyword in payment_keywords:
            if keyword in text:
                return 'payment'

        return 'delivery'

    def apply_webhook_status(self, status, description=None):
        """
        Aplica el estado del webhook externo a los estados internos.
        
        Para SUCCESS: Procesa pago, factura, stock y marca como exitoso.
        Para ERROR: Marca como error de pago o entrega según el tipo.
        
        Detecta webhooks duplicados y no los reprocesa.
        Rechaza webhooks SUCCESS que llegan después del tiempo límite (timeout + tolerancia).
        
        Returns:
            dict: Resultado estructurado de auditoría con claves:
                - processed (bool): Si se procesó efectivamente
                - result (str): 'processed', 'duplicate', 'late_arrival', 'internal_error'
                - order_status_before (str): vending_status antes del procesamiento
                - order_status_after (str): vending_status después del procesamiento
                - actions (dict): Acciones realizadas (payment, invoice, picking, bus, etc.)
        """
        self.ensure_one()

        order_status_before = self.vending_status
        audit = {
            'processed': False,
            'result': '',
            'order_status_before': order_status_before,
            'order_status_after': order_status_before,
            'actions': {},
        }

        _logger.info(f"Webhook recibido: status={status}, reference={self.vending_reference}")

        # Lock idempotente por orden usando pg_try_advisory_xact_lock.
        # Se libera automáticamente al COMMIT/ROLLBACK de la transacción.
        # Si otra transacción ya tomó el lock (retry de Odoo o reenvío de Winfas),
        # esta corta el procesamiento sin tocar nada — evita facturas/pagos/pickings duplicados.
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            (_VENDING_WEBHOOK_LOCK_NS, self.id),
        )
        if not self.env.cr.fetchone()[0]:
            _logger.warning(
                f"Webhook concurrente detectado para {self.vending_reference} "
                f"(advisory lock ya tomado por otra transacción)"
            )
            audit['result'] = 'duplicate_concurrent'
            return audit

        # Verificar webhook duplicado ANTES de cualquier procesamiento
        if self._check_webhook_duplicate():
            _logger.warning(f"Ignorando webhook duplicado para {self.vending_reference}")
            audit['result'] = 'duplicate'
            return audit

        # Para SUCCESS: verificar que estemos dentro del tiempo límite
        if status == 'SUCCESS':
            if not self._is_within_webhook_tolerance():
                _logger.warning(
                    f"[WEBHOOK] SUCCESS rechazado - fuera del tiempo límite: "
                    f"reference={self.vending_reference}"
                )
                # Registrar que llegó tarde para auditoría
                self.write({
                    'vending_internal_error': 'Webhook SUCCESS recibido fuera del tiempo límite. '
                                              'El pago no se procesó. '
                                              'Revisar logs del webhook y contactar a Winfas si es necesario.',
                    'vending_webhook_received_at': fields.Datetime.now(),
                })
                # Notificar a admin
                self.message_post(
                    body=_(
                        '⚠️ Webhook SUCCESS recibido fuera del tiempo límite. '
                        'El pago NO se procesó. Revisar con Winfas si el usuario pagó realmente.'
                    ),
                    message_type='notification',
                    subtype_xmlid='mail.mt_note',
                )
                audit['result'] = 'late_arrival'
                audit['order_status_after'] = self.vending_status
                return audit

            # Dentro del tiempo límite - procesar normalmente
            _logger.info(f"Iniciando procesamiento SUCCESS para order {self.vending_reference}")
            try:
                success_audit = self.process_vending_success_webhook()
                audit['processed'] = True
                audit['result'] = 'processed'
                audit['actions'] = success_audit.get('actions', {})
                audit['order_status_after'] = self.vending_status
                _logger.info(f"Webhook SUCCESS procesado correctamente para order {self.vending_reference}")
                return audit
            except Exception as e:
                _logger.error(f"Error procesando webhook SUCCESS para {self.vending_reference}: {e}")
                _logger.debug(f"Tipo de error: {type(e).__name__}, Detalles: {str(e)}")
                self.mark_as_delivery_error(error_description=str(e))
                audit['result'] = 'internal_error'
                audit['actions']['error'] = str(e)
                audit['order_status_after'] = self.vending_status
                return audit

        if status == 'ERROR':
            # Error real del webhook (máquina reportó fallo)
            error_desc = self._normalize_webhook_description(description)
            _logger.info(f"[APPLY WEBHOOK] Error recibido. error_desc cruda={error_desc}")
            
            error_type = self._infer_error_type_from_description(error_desc)
            _logger.info(f"[APPLY WEBHOOK] error_type inferido={error_type}")
            
            # Convertir descripción técnica a amigable para el usuario
            friendly_error_desc = self._get_user_friendly_error_description(error_desc)
            _logger.info(f"[APPLY WEBHOOK] friendly_error_desc={friendly_error_desc}")
            
            if error_type == 'payment':
                _logger.info(f"[APPLY WEBHOOK] Marcando como payment_error con descripción: {friendly_error_desc}")
                self.mark_as_payment_error(error_description=friendly_error_desc)
            else:
                _logger.info(f"[APPLY WEBHOOK] Marcando como delivery_error con descripción: {friendly_error_desc}")
                self.mark_as_delivery_error(error_description=friendly_error_desc)

            audit['processed'] = True
            audit['result'] = 'processed'
            audit['actions'] = {
                'error_type_inferred': error_type,
                'friendly_description': friendly_error_desc,
                'marked_as': f'{"payment_error" if error_type == "payment" else "vending_delivery_error"}',
            }
            audit['order_status_after'] = self.vending_status
            return audit

        audit['result'] = 'validation_error'
        return audit

    def _is_within_webhook_tolerance(self):
        """
        Verifica si la orden todavía está en tiempo válido para procesar webhooks.
        Permite un pequeño margen de tolerancia después del timeout para webhooks con latencia.
        
        Returns:
            bool: True si todavía se pueden procesar webhooks, False si ya expiró definitivamente
        """
        self.ensure_one()
        
        if not self.vending_qr_created_at:
            # Si no hay timestamp de creación, usar create_date como fallback
            _logger.warning(
                f"[TIMEOUT CHECK] vending_qr_created_at no existe para {self.vending_reference}, "
                f"usando create_date como fallback"
            )
            qr_created_at = self.create_date
        else:
            qr_created_at = self.vending_qr_created_at
        
        # Obtener timeout de la máquina
        if not self.vending_machine_id:
            _logger.warning(
                f"[TIMEOUT CHECK] No hay máquina asociada a {self.vending_reference}"
            )
            return True
        
        qr_timeout_seconds = self.vending_machine_id.qr_timeout_seconds or 60
        # Margen de tolerancia de 30 segundos para webhooks con latencia
        tolerance_seconds = 30
        
        # Calcular límite con tolerancia
        from datetime import timedelta
        timeout_end = qr_created_at + timedelta(seconds=qr_timeout_seconds + tolerance_seconds)
        
        now = fields.Datetime.now()
        is_within = now <= timeout_end
        
        _logger.info(
            f"[TIMEOUT CHECK] reference={self.vending_reference}: "
            f"qr_created={qr_created_at}, timeout={qr_timeout_seconds}s, "
            f"tolerance={tolerance_seconds}s, end={timeout_end}, now={now}, "
            f"within={is_within}"
        )
        
        return is_within

    def mark_as_qr_ready(self):
        """
        Marca la orden como QR listo y guarda el timestamp de creación.
        """
        self.ensure_one()
        self.write({
            'vending_status': 'qr_ready',
            'vending_qr_created_at': fields.Datetime.now(),
        })
        _logger.info(f"Order {self.vending_reference} marked as qr_ready")
        return True

    def mark_as_qr_expired(self):
        """
        Marca la orden como QR vencido.
        
        Transición: qr_ready → qr_expired cuando pasa el timeout configurado.
        """
        self.ensure_one()

        _logger.debug(f"[QR EXPIRED] Intentando marcar orden {self.vending_reference} como QR expirado")
        _logger.debug(f"[QR EXPIRED] Estado actual: {self.vending_status}")

        if self.vending_status in ('vending_delivery_success', 'vending_delivery_error', 'payment_success'):
            _logger.warning(
                f"[QR EXPIRED] Orden {self.vending_reference} no se puede marcar como QR expirado. "
                f"Estado actual: {self.vending_status}"
            )
            return False

        # Cancela la orden en el sistema de POS para que no quede en borrador
        self.sudo().write({
            'state': 'cancel',
            'vending_status': 'qr_expired',
            'vending_error_description': 'El tiempo para pagar ha finalizado. Por favor, vuelva a intentarlo.'
        })
        _logger.info(f"[QR EXPIRED] Orden {self.vending_reference} marcada como qr_expired")
        return True

    def _is_qr_expired(self):
        """
        Verifica si el QR de esta orden ha expirado basado en el timeout configurado.
        
        Returns:
            bool: True si el QR ha expirado, False en caso contrario
        """
        self.ensure_one()
        
        # Solo aplicable a órdenes en estado qr_ready
        if self.vending_status != 'qr_ready':
            return False
            
        if not self.vending_machine_id or not self.vending_machine_id.qr_timeout_seconds:
            return False
            
        # Calcular tiempo transcurrido desde la creación
        now = fields.Datetime.now()
        created_time = self.create_date
        elapsed_seconds = (now - created_time).total_seconds()
        timeout_seconds = self.vending_machine_id.qr_timeout_seconds
        
        is_expired = elapsed_seconds > timeout_seconds
        
        if is_expired:
            _logger.debug(
                f"[QR TIMEOUT] Orden {self.vending_reference} ha expirado. "
                f"Elapsed: {elapsed_seconds}s, Timeout: {timeout_seconds}s"
            )
            
        return is_expired

    @api.model
    def _expire_qr_orders(self):
        """
        Cron job que busca y expira automáticamente las órdenes con QR vencido.
        
        Transición: qr_ready → qr_expired cuando pasa el timeout configurado.
        """
        _logger.info("[QR TIMEOUT CRON] Iniciando verificación de QRs expirados")
        
        # Buscar órdenes en estado qr_ready que podrían estar expiradas
        candidate_orders = self.search([
            ('vending_status', '=', 'qr_ready'),
            ('vending_machine_id', '!=', False),
        ])
        
        _logger.debug(f"[QR TIMEOUT CRON] Encontradas {len(candidate_orders)} órdenes en qr_ready")
        
        expired_count = 0
        for order in candidate_orders:
            try:
                if order._is_qr_expired():
                    order.mark_as_qr_expired()
                    expired_count += 1
                    _logger.debug(
                        f"[QR TIMEOUT CRON] Orden {order.vending_reference} marcada como qr_expired"
                    )
            except Exception as e:
                _logger.error(
                    f"[QR TIMEOUT CRON] Error procesando orden {order.vending_reference}: {e}"
                )
                
        _logger.info(
            f"[QR TIMEOUT CRON] Verificación completada. {expired_count} órdenes expiradas"
        )
        
        return {'expired_count': expired_count}

    def mark_as_payment_error(self, error_description=None):
        """
        Marca la orden como error de pago.
        """
        self.ensure_one()

        if self.vending_status == 'vending_delivery_success':
            return False

        vals = {'vending_status': 'payment_error'}
        if error_description:
            vals['vending_error_description'] = error_description
            _logger.info(f"[PAYMENT ERROR] Guardando descripción: {error_description}")
        else:
            _logger.warning(f"[PAYMENT ERROR] Sin descripción de error")

        # Solo cancelar si sigue en draft; órdenes ya pagadas/finalizadas no permiten volver atrás.
        if self.state == 'draft':
            vals['state'] = 'cancel'
        self.sudo().write(vals)
        _logger.info(f"[PAYMENT ERROR] Order {self.vending_reference} marked as payment_error. vending_error_description={self.vending_error_description}")
        return True

    def mark_as_payment_success(self):
        """
        Marca la orden como pago exitoso.
        """
        self.ensure_one()

        terminal_statuses = {
            'vending_delivery_success',
            'vending_delivery_error',
            'payment_error',
            'qr_expired',
            'user_cancelled',
        }
        if self.vending_status in terminal_statuses:
            _logger.info(
                "Ignoring payment_success transition for %s because current status is %s",
                self.vending_reference,
                self.vending_status,
            )
            return False

        self.write({'vending_status': 'payment_success'})
        _logger.info(f"Order {self.vending_reference} marked as payment_success")
        return True

    def mark_as_delivery_success(self):
        """
        Marca la orden como entrega confirmada.
        """
        self.ensure_one()

        if self.vending_status == 'vending_delivery_success':
            _logger.info(
                f"Order {self.vending_reference} already in delivery success state, ignoring"
            )
            return False

        self.write({
            'vending_status': 'vending_delivery_success',
            'vending_webhook_received_at': fields.Datetime.now(),
        })
        _logger.info(f"Order {self.vending_reference} marked as vending_delivery_success")
        return True

    def mark_as_delivery_error(self, error_description=None):
        """
        Marca la orden como falla de entrega.
        """
        self.ensure_one()

        if self.vending_status == 'vending_delivery_success':
            _logger.warning(
                f"Attempted to mark successful order {self.vending_reference} as delivery error"
            )
            return False

        vals = {
            'vending_status': 'vending_delivery_error',
            'vending_webhook_received_at': fields.Datetime.now(),
        }
        if error_description:
            vals['vending_error_description'] = error_description
            _logger.info(f"[DELIVERY ERROR] Guardando descripción: {error_description}")
        else:
            _logger.warning(f"[DELIVERY ERROR] Sin descripción de error")

        # Solo cancelar si sigue en draft; órdenes ya pagadas/finalizadas no permiten volver atrás.
        if self.state == 'draft':
            vals['state'] = 'cancel'
        self.sudo().write(vals)
        _logger.info(f"[DELIVERY ERROR] Order {self.vending_reference} marked as vending_delivery_error. vending_error_description={self.vending_error_description}")
        return True

    def mark_as_user_cancelled(self):
        """
        Marca la orden como cancelada por el usuario.
        Se usa cuando el usuario sale de la pantalla de procesamiento sin completar el pago.
        """
        self.ensure_one()

        if self.vending_status in ('vending_delivery_success', 'vending_delivery_error', 'payment_success'):
            return False

        # Cancela la orden en el sistema de POS para que no quede en borrador
        self.sudo().write({
            'state': 'cancel',
            'vending_status': 'user_cancelled',
            'vending_error_description': 'El usuario canceló la operación.'
        })
        _logger.info(f"Order {self.vending_reference} marked as user_cancelled")
        return True

    def mark_as_success(self):
        """
        Alias de compatibilidad: marca la entrega como exitosa.
        """
        return self.mark_as_delivery_success()

    def mark_as_error(self, error_description=None):
        """
        Alias de compatibilidad: marca la entrega como error.
        """
        return self.mark_as_delivery_error(error_description=error_description)

    def _remap_order_lines_to_company(self, company):
        """
        Remapea los impuestos y cuentas de las líneas de la orden a la compañía indicada.

        Cuando el kiosk crea las líneas, usa los impuestos del producto, que pueden
        pertenecer a otra compañía. Odoo valida crossover de empresas al facturar,
        por lo que es necesario reemplazarlos por impuestos equivalentes de `company`.

        La búsqueda de equivalencia usa (name, type_tax_use, amount, amount_type).
        Si no se encuentra equivalente, se elimina el impuesto de la línea (mejor
        facturar sin impuesto que fallar completamente).

        Args:
            company: res.company — compañía destino para los impuestos/cuentas.
        """
        self.ensure_one()
        Tax = self.env['account.tax']

        for line in self.lines:
            if not line.tax_ids:
                continue

            new_tax_ids = []
            for tax in line.tax_ids:
                # Si ya pertenece a la compañía correcta (o es compartido), se conserva
                if not tax.company_id or tax.company_id == company:
                    new_tax_ids.append(tax.id)
                    continue

                # Buscar equivalente en la compañía destino por nombre + tipo + importe
                equivalent = Tax.search([
                    ('company_id', '=', company.id),
                    ('name', '=', tax.name),
                    ('type_tax_use', '=', tax.type_tax_use),
                    ('amount', '=', tax.amount),
                    ('amount_type', '=', tax.amount_type),
                ], limit=1)

                if equivalent:
                    _logger.info(
                        f"[REMAP TAX] Línea '{line.product_id.display_name}': "
                        f"impuesto '{tax.name}' ({tax.company_id.name}) → '{equivalent.name}' ({company.name})"
                    )
                    new_tax_ids.append(equivalent.id)
                else:
                    _logger.warning(
                        f"[REMAP TAX] No se encontró impuesto equivalente a '{tax.name}' "
                        f"en compañía '{company.name}'. Se omite el impuesto en esta línea."
                    )

            if set(new_tax_ids) != set(line.tax_ids.ids):
                line.write({'tax_ids': [fields.Command.set(new_tax_ids)]})
                _logger.debug(f"[REMAP TAX] tax_ids actualizados en línea {line.id}")

    def _process_vending_payment_and_invoice(self):
        """
        Procesa el pago y facturación para órdenes de vending exitosas.
        
        PRE-VALIDACIÓN (fuera del try-catch): Se realiza antes de intentar procesar.
        Si fallan, se registran como error interno y se retorna False.
        
        PROCESAMIENTO (dentro del try-catch): Se intenta crear pago/factura.
        Si fallan, se registran como error interno y se retorna False.
        
        Returns:
            bool: True si se procesó correctamente, False si hubo error interno.
                  NO lanza excepciones (todas se capturan).
        """
        self.ensure_one()
        
        _logger.info(f"Iniciando procesamiento de pago y factura para order {self.vending_reference}")
        
        # ===== PRE-VALIDACIÓN (antes de procesar) =====
        if not self.vending_machine_id:
            _logger.error(f"Order {self.vending_reference} no tiene máquina vending asociada")
            self._register_internal_error(
                'MISSING_MACHINE_CONFIG',
                'La orden no tiene máquina vending asociada'
            )
            return False
            
        machine = self.vending_machine_id
        _logger.debug(f"Máquina asociada: {machine.name}")

        if machine.is_fault_blocked:
            _logger.error(f"Máquina {machine.name} desactivada por falla")
            self._register_internal_error(
                'MACHINE_FAULT_BLOCKED',
                f'Máquina "{machine.name}" desactivada por falla'
            )
            return False
        
        if not machine.payment_method_id:
            _logger.error(f"Máquina {machine.name} no tiene método de pago configurado")
            self._register_internal_error(
                'PAYMENT_METHOD_NOT_CONFIGURED',
                f'Máquina "{machine.name}" no tiene método de pago configurado'
            )
            return False
            
        if not machine.invoice_journal_id:
            _logger.error(f"Máquina {machine.name} no tiene diario de facturas configurado")
            self._register_internal_error(
                'INVOICE_JOURNAL_NOT_CONFIGURED',
                f'Máquina "{machine.name}" no tiene diario de facturas configurado'
            )
            return False
        
        pos_config = self.session_id.config_id
        if machine.payment_method_id not in pos_config.payment_method_ids:
            _logger.error(
                f"Método de pago '{machine.payment_method_id.name}' no está en PdV '{pos_config.name}'"
            )
            self._register_internal_error(
                'PAYMENT_METHOD_NOT_IN_POS',
                f'Método de pago "{machine.payment_method_id.name}" no está configurado en PdV "{pos_config.name}"'
            )
            return False
        
        _logger.debug(f"Pre-validación completada exitosamente")
        
        # ===== PROCESAMIENTO (dentro del try-catch) =====
        try:
            # Forzar contexto de la compañía correcta
            order_in_company = self.with_company(machine.company_id)
            
            # Antes de facturar, asegurar que las líneas usen impuestos y cuentas
            # de la compañía correcta (machine.company_id). Las líneas pudo haberlas
            # creado el kiosk con impuestos del producto, que pueden ser de otra empresa.
            self._remap_order_lines_to_company(machine.company_id)
            
            # Crear o verificar pago
            _logger.debug(f"Verificando pagos existentes")
            if not order_in_company.payment_ids:
                _logger.debug(f"Creando pago para order {self.vending_reference}, monto: {order_in_company.amount_total}")
                payment_vals = {
                    'pos_order_id': order_in_company.id,
                    'amount': order_in_company.amount_total,
                    'payment_date': fields.Datetime.now(),
                    'payment_method_id': machine.payment_method_id.id,
                    'payment_status': 'done',
                }
                order_in_company.add_payment(payment_vals)
                _logger.info(f"Pago creado para order {self.vending_reference}")
            else:
                _logger.debug(f"Order {self.vending_reference} ya tiene pagos registrados")
            
            # Marcar como pagada
            _logger.debug(f"Marcando order {self.vending_reference} como pagada")
            if order_in_company.state != 'paid':
                order_in_company.action_pos_order_paid()
                _logger.info(f"Order {self.vending_reference} marcada como pagada")
            
            # Generar factura
            _logger.debug(f"Procesando facturación")
            if not order_in_company.account_move:
                _logger.debug(f"Generando nueva factura para order {self.vending_reference}")
                order_in_company.write({'to_invoice': True})
                
                # Cambiar diario temporalmente si es diferente
                original_journal = pos_config.invoice_journal_id
                if original_journal != machine.invoice_journal_id:
                    _logger.debug(f"Cambiando diario a: {machine.invoice_journal_id.name}")
                    pos_config.write({'invoice_journal_id': machine.invoice_journal_id.id})
                
                try:
                    invoice = order_in_company._generate_pos_order_invoice()
                    _logger.info(f"Factura creada: {invoice.name}")
                    if invoice:
                        invoice.write({'vending_order_id': order_in_company.id})
                finally:
                    # Restaurar diario original
                    if original_journal:
                        _logger.debug(f"Restaurando diario original: {original_journal.name}")
                        pos_config.write({'invoice_journal_id': original_journal.id})
            else:
                _logger.debug(f"Order {self.vending_reference} ya tiene factura: {order_in_company.account_move.name}")
            
            _logger.info(f"Procesamiento de pago y factura completado correctamente")
            return True
            
        except Exception as e:
            _logger.error(f"Error procesando pago/factura para order {self.vending_reference}: {e}")
            self._register_internal_error(
                'PAYMENT_INVOICE_PROCESSING_ERROR',
                f'Error al procesar pago/factura: {str(e)}'
            )
            return False

    def _process_vending_stock_movement(self):
        """
        Procesa el movimiento de stock para vending creando un picking de entrega.
        
        PRE-VALIDACIÓN (fuera del try-catch): Se realiza antes de intentar procesar.
        Si fallan, se registran como error interno y se retorna False.
        
        PROCESAMIENTO (dentro del try-catch): Se intenta crear picking y moves.
        Si fallan, se registran como error interno, se cancela el picking y se retorna False.
        
        Returns:
            bool: True si se procesó correctamente, False si hubo error interno.
                  NO lanza excepciones (todas se capturan).
        """
        self.ensure_one()
        
        _logger.info(f"Iniciando procesamiento de stock para order {self.vending_reference}")
        
        # ===== PRE-VALIDACIÓN (antes de procesar) =====
        if not self.vending_machine_id:
            _logger.error(f"La orden {self.vending_reference} no tiene máquina vending asociada")
            self._register_internal_error(
                'MISSING_MACHINE_CONFIG',
                'La orden no tiene máquina vending asociada'
            )
            return False
            
        machine = self.vending_machine_id
        _logger.debug(f"Máquina asociada: {machine.name}")
        
        if not machine.anonymous_partner_id:
            _logger.error(f"Máquina {machine.name} no tiene cliente anónimo configurado")
            self._register_internal_error(
                'ANONYMOUS_PARTNER_NOT_CONFIGURED',
                f'Máquina "{machine.name}" no tiene cliente anónimo configurado'
            )
            return False
        
        if not self.vending_slot_id:
            _logger.error(f"La orden {self.vending_reference} no tiene slot de vending asociado")
            self._register_internal_error(
                'MISSING_SLOT_CONFIG',
                'La orden no tiene slot de vending asociado'
            )
            return False
            
        slot = self.vending_slot_id
        _logger.debug(f"Slot asociado: {slot.name}")

        if slot.is_fault_blocked:
            _logger.error(f"Slot {slot.name} desactivado por falla")
            self._register_internal_error(
                'SLOT_FAULT_BLOCKED',
                f'Slot "{slot.name}" desactivado por falla'
            )
            return False
        
        if not slot.location_id:
            _logger.error(f"Slot {slot.name} no tiene ubicación de stock configurada")
            self._register_internal_error(
                'SLOT_LOCATION_NOT_CONFIGURED',
                f'Slot "{slot.name}" no tiene ubicación de stock configurada'
            )
            return False
        
        warehouse = machine.warehouse_id
        if not warehouse:
            _logger.error(f"Máquina {machine.name} no tiene almacén asociado")
            self._register_internal_error(
                'WAREHOUSE_NOT_CONFIGURED',
                f'Máquina "{machine.name}" no tiene almacén asociado'
            )
            return False
        
        delivery_pick_type = warehouse.out_type_id
        if not delivery_pick_type:
            _logger.error(f"Almacén {warehouse.name} no tiene tipo de entrega configurado")
            self._register_internal_error(
                'DELIVERY_PICK_TYPE_NOT_CONFIGURED',
                f'Almacén "{warehouse.name}" no tiene tipo de operación de entrega configurado'
            )
            return False
        
        # Verificar si hay productos stockeables
        stockable_lines = self.lines.filtered(lambda line: line.product_id.is_storable == True)
        
        if not stockable_lines:
            _logger.info(f"No hay productos stockeables en la orden, completando sin movimiento de stock")
            return True
        
        _logger.debug(f"Productos stockeables encontrados: {len(stockable_lines)}")
        _logger.debug(f"Pre-validación completada exitosamente")
        
        # ===== PROCESAMIENTO (dentro del try-catch) =====
        picking = None
        try:
            # El almacén (y sus ubicaciones/tipos de operación) define la compañía del stock.
            # Usar warehouse.company_id para todos los objetos de stock garantiza consistencia.
            # La compañía del POS (machine.company_id) se usa solo para el pago/factura.
            stock_company = warehouse.company_id or machine.company_id
            _logger.debug(f"Compañía para movimientos de stock: {stock_company.name} (almacén: {warehouse.name})")

            env_in_company = self.env(context=dict(self.env.context, allowed_company_ids=[stock_company.id]))
            order_in_company = self.with_company(machine.company_id)
            
            # Crear picking de entrega
            picking_vals = {
                'partner_id': machine.anonymous_partner_id.id,
                'picking_type_id': delivery_pick_type.id,
                'location_id': slot.location_id.id,
                'location_dest_id': delivery_pick_type.default_location_dest_id.id,
                'origin': order_in_company.name,
                'state': 'draft',
                'move_type': 'direct',
                'vending_order_id': order_in_company.id,
                'company_id': stock_company.id,
            }
            
            _logger.debug(f"Creando picking de entrega")
            picking = env_in_company['stock.picking'].create(picking_vals)
            _logger.debug(f"Picking creado: {picking.name} (ID: {picking.id})")
            
            # Asignar picking a la orden
            order_in_company.write({'vending_delivery_id': picking.id})
            _logger.debug(f"Picking asignado a vending_delivery_id")
            
            # Crear líneas del picking
            for line in stockable_lines:
                _logger.debug(f"Procesando línea: {line.product_id.display_name}, Qty: {line.qty}")
                
                # Verificar stock disponible
                available_qty = env_in_company['stock.quant']._get_available_quantity(
                    line.product_id,
                    slot.location_id
                )
                
                _logger.debug(f"Stock disponible: {available_qty}, Requerido: {line.qty}")
                
                if available_qty < line.qty:
                    _logger.error(f"Stock insuficiente para {line.product_id.display_name}")
                    order_in_company._register_internal_error(
                        'INSUFFICIENT_STOCK',
                        f'Stock insuficiente para "{line.product_id.display_name}" en slot "{slot.name}". '
                        f'Disponible: {available_qty}, Requerido: {line.qty}'
                    )
                    return False
                
                # Crear línea de movimiento con la compañía del almacén
                move_vals = {
                    'product_id': line.product_id.id,
                    'product_uom': line.product_id.uom_id.id,
                    'product_uom_qty': line.qty,
                    'picking_id': picking.id,
                    'location_id': slot.location_id.id,
                    'location_dest_id': delivery_pick_type.default_location_dest_id.id,
                    'origin': order_in_company.name,
                    'company_id': stock_company.id,
                }
                
                _logger.debug(f"Creando línea de movimiento")
                move = env_in_company['stock.move'].create(move_vals)
                _logger.debug(f"Línea creada: (ID: {move.id})")
            
            # Confirmar picking
            _logger.debug(f"Confirmando picking {picking.name}")
            picking.action_confirm()
            
            # Asignar stock
            _logger.debug(f"Asignando stock al picking")  
            picking.action_assign()
            
            # Validar picking
            if picking.state == 'assigned':
                _logger.debug(f"Validando entrega")
                picking.button_validate()
                _logger.info(f"Picking validado: {picking.name}")
                
                # FORZAR notificación bus de productos
                # button_validate() puede usar contexto especial que omite hooks de write/unlink
                # Por eso notificamos explícitamente aquí
                _logger.debug(f"Forzando notificación bus de productos para máquina {machine.name}")
                affected_machines = env_in_company['vending.machine'].browse(machine.id)
                env_in_company['stock.quant']._notify_vending_changes_for_machines(affected_machines)
            else:
                _logger.warning(f"Stock no completamente asignado al picking: estado={picking.state}")
                order_in_company._register_internal_error(
                    'STOCK_ASSIGNMENT_FAILED',
                    f'No se pudo asignar completamente el stock. Estado: {picking.state}'
                )
                return False
            
            _logger.info(f"Procesamiento de stock completado correctamente")
            return True
            
        except Exception as e:
            _logger.error(f"Error en procesamiento de stock: {e}")
            
            # Intentar cancelar picking si se creó
            if picking:
                try:
                    if picking.state not in ('done', 'cancel'):
                        picking.action_cancel()
                        _logger.debug(f"Picking {picking.name} cancelado")
                except Exception as cancel_error:
                    _logger.error(f"No se pudo cancelar picking: {cancel_error}")
            
            # Usar self original ya que order_in_company puede no estar definido si falla temprano
            self._register_internal_error(
                'STOCK_PROCESSING_ERROR',
                f'Error al procesar movimiento de stock: {str(e)}'
            )
            return False

    def process_vending_success_webhook(self):
        """
        Procesa el webhook de éxito de vending: pago, factura, stock y estados.
        
        FILOSOFÍA:
        - El webhook SUCCESS significa que la máquina YA despachó el producto.
        - Aunque Odoo falle internamente, la entrega fue exitosa desde el punto de vista de la máquina.
        - Los errores internos de Odoo se registran en vending_internal_error, NO cambian vending_status.
        - Cada procesamiento (pago, stock, marking) está en su propio try-catch.
        - SIEMPRE retorna dict de auditoría con processed=True.
        
        Returns:
            dict: Resultado de auditoría con claves:
                - processed (bool): SIEMPRE True (el webhook fue SUCCESS)
                - actions (dict): Detalle de cada acción ejecutada y su resultado
        """
        self.ensure_one()
        
        actions = {}
        
        _logger.info(f"=== Procesando webhook SUCCESS para order {self.vending_reference}")
        
        # Paso 1: Procesar pago y factura
        _logger.info(f"Paso 1/3: Procesando pago y factura")
        try:
            payment_ok = self._process_vending_payment_and_invoice()
            actions['payment_created'] = payment_ok
            if payment_ok:
                _logger.info(f"Paso 1/3: Pago y factura procesados correctamente")
                # Capturar nombres de factura/pago generados
                if self.account_move:
                    actions['invoice_name'] = self.account_move.name
                if self.payment_ids:
                    actions['payment_amount'] = self.amount_total
            else:
                _logger.warning(f"Paso 1/3: Falló pero error ya registrado internamente")
                actions['payment_error'] = self.vending_internal_error or 'Error desconocido'
        except Exception as e:
            _logger.error(f"Paso 1/3: Error inesperado (no debería ocurrir): {e}")
            self._register_internal_error(
                'UNEXPECTED_PAYMENT_ERROR',
                f'Error inesperado en pago: {str(e)}'
            )
            actions['payment_created'] = False
            actions['payment_error'] = str(e)
        
        # Paso 2: Procesar stock
        _logger.info(f"Paso 2/3: Procesando movimiento de stock")
        try:
            stock_ok = self._process_vending_stock_movement()
            actions['stock_moved'] = stock_ok
            if stock_ok:
                _logger.info(f"Paso 2/3: Stock procesado correctamente")
                if self.vending_delivery_id:
                    actions['picking_name'] = self.vending_delivery_id.name
            else:
                _logger.warning(f"Paso 2/3: Falló pero error ya registrado internamente")
                actions['stock_error'] = self.vending_internal_error or 'Error desconocido'
        except Exception as e:
            _logger.error(f"Paso 2/3: Error inesperado (no debería ocurrir): {e}")
            self._register_internal_error(
                'UNEXPECTED_STOCK_ERROR',
                f'Error inesperado en stock: {str(e)}'
            )
            actions['stock_moved'] = False
            actions['stock_error'] = str(e)
        
        # Paso 3: Marcar como entrega exitosa (SIEMPRE se intenta)
        _logger.info(f"Paso 3/3: Marcando como entrega exitosa")
        try:
            self.mark_as_delivery_success()
            actions['marked_delivery_success'] = True
            _logger.info(f"Paso 3/3: Order marcada como vending_delivery_success")
        except Exception as e:
            _logger.error(f"Paso 3/3: Error marcando como delivery_success: {e}")
            self._register_internal_error(
                'DELIVERY_SUCCESS_MARKING_ERROR',
                f'Error marcando como entrega exitosa: {str(e)}'
            )
            actions['marked_delivery_success'] = False
            actions['marking_error'] = str(e)
        
        _logger.info(f"=== Webhook SUCCESS completado para order {self.vending_reference}")
        
        return {
            'processed': True,
            'actions': actions,
        }

    def action_open_stock_picking(self):
        """Abre el registro de stock.picking asociado a la orden de vending."""
        if not self.vending_delivery_id:
            raise UserError(
                _('No hay operación de stock asociada a esta orden')
            )
        
        return {
            'type': 'ir.actions.act_window',
            'name': _('Operación Stock'),
            'res_model': 'stock.picking',
            'res_id': self.vending_delivery_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
        }


class PosOrderLine(models.Model):
    """Compatibilidad para vistas que esperan nombre de producto traducido."""

    _inherit = 'pos.order.line'

    translated_product_name = fields.Char(
        string='Translated Product Name',
        related='product_id.name',
        readonly=True,
    )
    
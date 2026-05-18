# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""
Extensión de pos.config para vending machines.
"""

import logging
import pytz # type: ignore
from collections import defaultdict
from odoo import models, fields, api, _  # type: ignore
from odoo.exceptions import ValidationError  # type: ignore

_logger = logging.getLogger(__name__)


class PosConfig(models.Model):
    _inherit = 'pos.config'

    def get_vending_catalog_data(self):
        """
        Retorna datos de catálogo vending ordenados por menor code de slot.

        Solo incluye productos que tengan al menos un slot activo,
        con ubicación y stock mayor a 0.
        """
        self.ensure_one()

        empty_result = {
            'product_ids': [],
            'product_slots': {},
            'product_min_slot_code': {},
        }

        if not self.vending_machine_id:
            return empty_result

        if self.vending_machine_id.is_fault_blocked:
            return empty_result

        all_slots = self.env['vending.slot'].search([
            ('machine_id', '=', self.vending_machine_id.id),
            ('is_active', '=', True),
            ('is_fault_blocked', '=', False),
            ('location_id', '!=', False),
        ], order='code, id')

        slots_with_stock = all_slots.filtered(lambda s: s.current_stock > 0 and s.product_tmpl_id)
        company_id = self.company_id.id

        product_slots = {}
        product_min_slot_code = {}
        product_name_by_id = {}

        for slot in slots_with_stock:
            product = slot.product_tmpl_id
            if product.company_id and product.company_id.id != company_id:
                continue

            product_id = product.id
            if product_id not in product_slots:
                product_slots[product_id] = []
                product_min_slot_code[product_id] = slot.code
                product_name_by_id[product_id] = (product.display_name or product.name or '').lower()

            product_slots[product_id].append({
                'code': slot.code,
                'name': slot.name,
                'stock': slot.current_stock,
            })

        sorted_product_ids = sorted(
            product_slots.keys(),
            key=lambda product_id: (
                product_min_slot_code[product_id],
                product_name_by_id.get(product_id, ''),
                product_id,
            )
        )

        _logger.info(
            "[Vending] Catálogo máquina %s: %s productos ordenados por slot mínimo",
            self.vending_machine_id.name,
            len(sorted_product_ids),
        )

        return {
            'product_ids': sorted_product_ids,
            'product_slots': product_slots,
            'product_min_slot_code': product_min_slot_code,
        }

    vending_machine_id = fields.Many2one(
        'vending.machine',
        string='Máquina Expendedora',
        help='Máquina expendedora asociada a este punto de venta'
    )
    vending_countdown_seconds = fields.Integer(
        string='Tiempo de espera vending (segundos)',
        related='vending_machine_id.countdown_seconds',
        readonly=False,
        help='Tiempo en segundos antes de volver automáticamente al menú principal tras una operación'
    )
    vending_qr_timeout_seconds = fields.Integer(
        string='Timeout de QR vending (segundos)',
        related='vending_machine_id.qr_timeout_seconds',
        readonly=False,
        help='Tiempo en segundos de vida del QR de pago antes de expirar'
    )
    vending_invoice_journal_id = fields.Many2one(
        'account.journal',
        string='Diario de facturas vending',
        related='vending_machine_id.invoice_journal_id',
        readonly=True,
        help='Diario donde se crearán las facturas de vending'
    )
    vending_machine_fault_blocked = fields.Boolean(
        string='Máquina desactivada por falla',
        related='vending_machine_id.is_fault_blocked',
        readonly=True,
    )
    vending_machine_has_fault_blocked_slots = fields.Boolean(
        string='Tiene slots desactivados por falla',
        related='vending_machine_id.has_fault_blocked_slots',
        readonly=True,
    )
    vending_machine_fault_blocked_slots_count = fields.Integer(
        string='Cantidad de slots desactivados por falla',
        related='vending_machine_id.fault_blocked_slots_count',
        readonly=True,
    )

    def write(self, vals):
        """Sincronizar relación bidireccional POS-Vending al escribir."""
        # Solo sincronizar si no estamos en un contexto de sincronización
        if 'vending_machine_id' in vals and not self.env.context.get('skip_vending_sync'):
            # Limpiar referencias anteriores ANTES del write
            for record in self:
                if record.vending_machine_id:
                    record.vending_machine_id.with_context(skip_pos_sync=True).write({'pos_config_id': False})
        
        result = super().write(vals)
        
        # Establecer nueva referencia DESPUÉS del write
        if 'vending_machine_id' in vals and not self.env.context.get('skip_vending_sync'):
            for record in self:
                if record.vending_machine_id:
                    record.vending_machine_id.with_context(skip_pos_sync=True).write({
                        'pos_config_id': record.id
                    })
        
        return result

    def get_available_vending_products(self):
        """
        Retorna productos disponibles para esta máquina expendedora con stock real > 0.
        Solo se usa cuando self_ordering_mode == 'vending'.
        """
        self.ensure_one()
        
        if not self.vending_machine_id:
            _logger.warning("[Vending] get_available_vending_products: No hay máquina configurada")
            return self.env['product.template'].browse()

        catalog_data = self.get_vending_catalog_data()
        return self.env['product.template'].browse(catalog_data['product_ids'])
    
    def get_available_vending_product_ids(self):
        """
        Retorna solo los IDs de productos disponibles con stock > 0.
        Versión optimizada para llamadas desde frontend vía RPC.
        """
        return self.get_vending_catalog_data()['product_ids']
    
    def get_best_slot_for_product(self, product_tmpl_id):
        """
        Retorna el slot con mayor stock disponible para el producto.
        """
        self.ensure_one()
        
        if not self.vending_machine_id:
            return self.env['vending.slot'].browse()

        if self.vending_machine_id.is_fault_blocked:
            return self.env['vending.slot'].browse()
        
        slots = self.env['vending.slot'].search([
            ('machine_id', '=', self.vending_machine_id.id),
            ('product_tmpl_id', '=', product_tmpl_id),
            ('is_active', '=', True),
            ('is_fault_blocked', '=', False),
            ('location_id', '!=', False),
            ('current_stock', '>', 0),
        ], order='current_stock desc', limit=1)
        
        return slots

    def get_slots_for_product(self, product_tmpl_id):
        """
        Retorna todos los slots disponibles con stock para un producto.
        Usado para mostrar en el catálogo de productos.
        """
        self.ensure_one()
        
        if not self.vending_machine_id:
            return []

        if self.vending_machine_id.is_fault_blocked:
            return []
        
        slots = self.env['vending.slot'].search([
            ('machine_id', '=', self.vending_machine_id.id),
            ('product_tmpl_id', '=', product_tmpl_id),
            ('is_active', '=', True),
            ('is_fault_blocked', '=', False),
            ('location_id', '!=', False),
            ('current_stock', '>', 0),
        ], order='code')
        
        return [{
            'code': slot.code,
            'name': slot.name,
            'stock': slot.current_stock,
        } for slot in slots]

    def get_all_product_slots(self):
        """
        Retorna un diccionario con los slots disponibles para cada producto.
        Optimizado para cargar todo de una vez en el frontend.
        
        Returns:
            dict: {product_id: [{'code': int, 'name': str, 'stock': float}, ...]}
        """
        self.ensure_one()
        
        return self.get_vending_catalog_data()['product_slots']

    def get_product_min_slot_code_map(self):
        """
        Retorna el menor code de slot activo con stock por producto.

        Returns:
            dict: {product_id: min_slot_code}
        """
        self.ensure_one()
        return self.get_vending_catalog_data()['product_min_slot_code']

    @api.model
    def _load_pos_self_data_search_read(self, response, config):
        """
        Extender la carga de datos para filtrar productos en modo vending.
        """
        records = super()._load_pos_self_data_search_read(response, config)
        
        _logger.info("[Vending] _load_pos_self_data_search_read (pos.config) - modo: %s", 
                     config.self_ordering_mode)
        
        # Si está en modo vending, agregar información específica
        if config.self_ordering_mode == 'vending':
            if not config.vending_machine_id:
                # Si no hay máquina configurada, marcar para mostrar mensaje
                _logger.warning("[Vending] No hay máquina configurada para POS %s", config.id)
                records[0]['_vending_no_machine'] = True
                records[0]['_vending_available_products'] = []
                records[0]['_vending_product_slots'] = {}
                records[0]['vending_countdown_seconds'] = 40  # Valor por defecto
                records[0]['vending_qr_timeout_seconds'] = 120  # Valor por defecto
            else:
                # Obtener catálogo vending ordenado por menor slot.
                catalog_data = config.get_vending_catalog_data()
                machine = config.vending_machine_id
                records[0]['_vending_available_products'] = catalog_data['product_ids']
                product_slots = catalog_data['product_slots']
                records[0]['_vending_product_slots'] = product_slots
                records[0]['_vending_product_min_slot_code'] = catalog_data['product_min_slot_code']
                records[0]['_vending_machine_id'] = config.vending_machine_id.id
                records[0]['_vending_machine_fault_blocked'] = bool(machine.is_fault_blocked)
                records[0]['_vending_machine_has_fault_blocked_slots'] = bool(machine.has_fault_blocked_slots)
                records[0]['_vending_machine_fault_blocked_slots_count'] = machine.fault_blocked_slots_count or 0
                records[0]['vending_countdown_seconds'] = config.vending_countdown_seconds or 40
                records[0]['vending_qr_timeout_seconds'] = config.vending_qr_timeout_seconds or 120
                _logger.info("[Vending] Enviando al frontend _vending_available_products: %s", 
                             catalog_data['product_ids'])
                _logger.info("[Vending] Enviando al frontend _vending_product_slots: %s productos con slots", 
                             len(product_slots))
                
        return records
    
    def get_statistics_for_session(self, session):
        """
        Parche para el método original de odoo. Ahora no se consideran 
        órdenes que están en estado draft pero que tienen un estado de vending que indica que no se completarán (qr_expired, user_cancelled, payment_error, vending_delivery_error).
        Esto es porque en vending el proceso de pago es externo y puede haber órdenes que queden en draft pero que no se completarán nunca, lo que distorsionaría las estadísticas si se contaran como órdenes activas.
        """
        self.ensure_one()
        currency = self.currency_id
        timezone = pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'UTC')
        statistics = {
            'cash': {
                'raw_opening_cash': session.cash_register_balance_start,
                'opening_cash': currency.format(session.cash_register_balance_start)
            },
            'date': {
                'is_started': bool(session.start_at),
                'start_date': session.start_at.astimezone(timezone).strftime('%b %d') if session.start_at else False,
            },
            'orders': {
                'paid': False,
                'draft': False,
            },
        }

        all_paid_orders = session.order_ids.filtered(lambda o: o.state in ['paid', 'done'])
        refund_orders = all_paid_orders.filtered(lambda o: o.is_refund)
        draft_orders = session.order_ids.filtered(lambda o: (o.state == 'draft' and not o.vending_status in ['qr_expired', 'user_cancelled', 'payment_error', 'vending_delivery_error']))
        non_refund_orders = all_paid_orders - refund_orders

        # calculate total refunded amount per original order for refund count check
        refund_totals = defaultdict(float)
        for refund in refund_orders:
            if refund.refunded_order_id:
                refund_totals[refund.refunded_order_id.id] += abs(refund.amount_total)

        # count paid orders that are not completely refunded
        paid_order_count = sum(
            1 for order in non_refund_orders
            if refund_totals.get(order.id, 0.0) != order.amount_total
        )

        if paid_order_count:
            total_paid = sum(all_paid_orders.mapped('amount_total'))
            statistics['orders']['paid'] = {
                'amount': total_paid,
                'count': paid_order_count,
                'display': f"{currency.format(total_paid)} ({paid_order_count} {'order' if paid_order_count == 1 else 'orders'})"
            }

        if draft_orders:
            total_draft = sum(draft_orders.mapped('amount_total'))
            count_draft = len(draft_orders)
            statistics['orders']['draft'] = {
                'amount': total_draft,
                'count': count_draft,
                'display': f"{currency.format(total_draft)} ({count_draft} {'order' if count_draft == 1 else 'orders'})"
            }

        return statistics

# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution, third party addon
#    Copyright (C) 2004-2015 Vertel AB (<http://vertel.se>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp import models, fields, api, _, tools
from openerp.exceptions import except_orm, Warning, RedirectWarning
from openerp import SUPERUSER_ID
from lxml import etree
import urlparse
import urllib2
import base64
import hashlib
import logging
import pprint
from openerp.addons.payment_payer_se.controllers.main import PayerSEController
from openerp.addons.payment.models.payment_acquirer import ValidationError

_logger = logging.getLogger(__name__)

class AcquirerPayerSE(models.Model):
    _inherit = 'payment.acquirer'
    
    payerse_agent_id          = fields.Char(string='Payer.se Agent ID', required_if_provider='payerse')
    payerse_key_1              = fields.Char(string='Payer.se Key 1/Key A', help='The first preshared key.', required_if_provider='payerse')
    payerse_key_2              = fields.Char(string='Payer.se Key 2/Key B', help='The second preshared key.', required_if_provider='payerse')
    payerse_payment_method_card = fields.Boolean(string='Allow card payments.', help='Allow card payment.')
    payerse_payment_method_bank = fields.Boolean(string='Allow bank payments.', help='Allow bank payment.')
    payerse_payment_method_phone = fields.Boolean(string='Allow phone payments.', help='Allow phone payment.')
    payerse_payment_method_invoice = fields.Boolean(string='Allow invoice payments.', help='Allow card payment.')
    payerse_return_address      = fields.Char(string='Success return address', help='Default return address when payment is successfull.', default='/shop/payment/validate', required_if_provider='payerse')
    payerse_cancel_address      = fields.Char(string='Cancellation return address', help='Default return address when payment is cancelled.', default='/shop/payment', required_if_provider='payerse')
    
    _ip_whitelist = ["79.136.103.5", "94.140.57.180", "94.140.57.181", "94.140.57.184"]
    
    def validate_ip(self, ip):
        if ip in self._ip_whitelist:
            return True
        _logger.warning('Payer.se: callback from unauthorized ip: %s' % ip)
        return False
    
    def _get_providers(self, cr, uid, context=None):
        providers = super(AcquirerPayerSE, self)._get_providers(cr, uid, context=context)
        providers.append(['payerse', 'Payer.se'])
        return providers
    
    @api.v8
    def _payerse_generate_xml_data(self, partner_values, tx_values, order):
        root = etree.Element("payread_post_api_0_2", nsmap={
            "xsi": "http://www.w3.org/2001/XMLSchema-instance",
        }, attrib={
            "{http://www.w3.org/2001/XMLSchema-instance}"
            "noNamespaceSchemaLocation": "payread_post_api_0_2.xsd",
        })
        #Generate seller data
        seller_details = etree.SubElement(root, "seller_details")
        etree.SubElement(seller_details, "agent_id").text = self.payerse_agent_id
        
        #Generate buyer data
        buyer_details = etree.SubElement(root, "buyer_details")
        etree.SubElement(buyer_details, "first_name").text = partner_values['first_name']
        etree.SubElement(buyer_details, "last_name").text = partner_values['last_name']
        etree.SubElement(buyer_details, "adress_line_1").text = partner_values['address']
        #etree.SubElement(buyer_details, "adress_line_2")    #Necessary?
        etree.SubElement(buyer_details, "postal_code").text = partner_values['zip']
        etree.SubElement(buyer_details, "city").text = partner_values['city']
        etree.SubElement(buyer_details, "country_code").text = partner_values['country'].code
        etree.SubElement(buyer_details, "phone_home").text = partner_values['phone']
        #etree.SubElement(buyer_details, "phone_work").text = partner_values['phone']
        #etree.SubElement(buyer_details, "phone_mobile").text = partner_values['phone']
        etree.SubElement(buyer_details, "email").text = partner_values['email']
        #etree.SubElement(buyer_details, "organisation").text = partner_values['first_name']
        #etree.SubElement(buyer_details, "orgnr").text = partner_values['first_name']
        
        #Generate purchase data
        purchase = etree.SubElement(root, "purchase")
        etree.SubElement(purchase, "currency").text = "SEK" #tx_values['currency'].name
        etree.SubElement(purchase, "description").text = tx_values['reference']
        etree.SubElement(purchase, "reference_id").text = tx_values['reference']
        purchase_list = etree.SubElement(purchase, "purchase_list")
        
        #Generate product lines
        i = 1
        for line in order.order_line:
            tax = order._amount_line_tax(line)
            freeform_purchase = etree.SubElement(purchase_list, "freeform_purchase")
            etree.SubElement(freeform_purchase, "line_number").text = unicode(i)
            if line.product_uom_qty.is_integer():
                etree.SubElement(freeform_purchase, "description").text = line.name
                quantity = line.product_uom_qty
            else:
                etree.SubElement(freeform_purchase, "description").text = '%d X %s' % (line.product_uom_qty, line.name)
                quantity = 1.0
            etree.SubElement(freeform_purchase, "price_including_vat").text = unicode((line.price_subtotal + tax) / quantity)
            etree.SubElement(freeform_purchase, "vat_percentage").text = unicode(tax * 100 / line.price_subtotal)
            etree.SubElement(freeform_purchase, "quantity").text = unicode(int(quantity))
            i += 1
        
        #Generate callback data
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        processing_control = etree.SubElement(root, "processing_control")
        etree.SubElement(processing_control, "success_redirect_url").text = urlparse.urljoin(base_url, tx_values.get('return_url', ''))
        etree.SubElement(processing_control, "authorize_notification_url").text = urlparse.urljoin(base_url, PayerSEController._verify_url)
        etree.SubElement(processing_control, "settle_notification_url").text = urlparse.urljoin(base_url, PayerSEController._verify_url)
        etree.SubElement(processing_control, "redirect_back_to_shop_url").text = urlparse.urljoin(base_url, tx_values.get('cancel_url', ''))
        
        #Generate other data
        
        database_overrides = etree.SubElement(root, "database_overrides")
        payment_methods = etree.SubElement(database_overrides, "accepted_payment_methods")
        if self.payerse_payment_method_bank:
            etree.SubElement(payment_methods, "payment_method").text = "bank"
        if self.payerse_payment_method_card:
            etree.SubElement(payment_methods, "payment_method").text = "card"
        if self.payerse_payment_method_invoice:
            etree.SubElement(payment_methods, "payment_method").text = "invoice"
        if self.payerse_payment_method_phone:
            etree.SubElement(payment_methods, "payment_method").text = "phone"
        
        if self.environment == "test":
            etree.SubElement(database_overrides, "test_mode").text = "true"
        else:
            etree.SubElement(database_overrides, "test_mode").text = "false"
        
        #TODO: how and when to use debug mode?
        etree.SubElement(database_overrides, "debug_mode").text = "verbose"
        
        #TODO: Add support for other languages
        etree.SubElement(database_overrides, "language").text = "sv"
        
        _logger.info(etree.tostring(root, pretty_print=True))
        
        return base64.b64encode(etree.tostring(root, pretty_print=False))
    
    def _payerse_generate_checksum(self, data):
        return hashlib.md5(self.payerse_key_1 + data + self.payerse_key_2).hexdigest()
    
    @api.multi
    def payerse_form_generate_values(self, partner_values, tx_values):
        """method that generates the values used to render the form button template."""
        self.ensure_one()
        _logger.info(pprint.pformat(partner_values))
        _logger.info(pprint.pformat(tx_values))
        
        #TODO: Add alternative to using sale order (keys in tx_values?).
        reference = tx_values['reference']
        order = self.env['sale.order'].search([['name', '=', reference]])
        
        xml_data = self._payerse_generate_xml_data(partner_values, tx_values, order)
        
        payer_tx_values = dict(tx_values)
        payer_tx_values.update({
            'payer_agentid': self.payerse_agent_id,
            'payer_xml_writer': "payer_php_0_2_v27",
            'payer_data': xml_data,
            'payer_charset': "UTF-8",
            'payer_checksum': self._payerse_generate_checksum(xml_data),
            'payer_testmode': self.environment,
        })
        
        if not payer_tx_values['return_url']:
            payer_tx_values['return_url'] = self.payerse_return_address
        if not payer_tx_values['return_url']:
            payer_tx_values['return_url'] = self.payerse_return_address
        return partner_values, payer_tx_values
    
    @api.multi
    def payerse_get_form_action_url(self):
        """method that returns the url of the button form. It is used for example in
        ecommerce application, if you want to post some data to the acquirer."""
        return 'https://secure.payer.se/PostAPI_V1/InitPayFlow'
    
    @api.multi
    def payerse_compute_fees(self, amount, currency_id, country_id):
        """computed the fees of the acquirer, using generic fields
        defined on the acquirer model (see fields definition)."""
        self.ensure_one()
        if not self.fees_active:
            return 0.0
        country = self.env['res.country'].browse(country_id)
        if country and self.company_id.country_id.id == country.id:
            percentage = self.fees_dom_var
            fixed = self.fees_dom_fixed
        else:
            percentage = self.fees_int_var
            fixed = self.fees_int_fixed
        fees = (percentage / 100.0 * amount + fixed ) / (1 - percentage / 100.0)
        return fees


class TxPayerSE(models.Model):
    _inherit = 'payment.transaction'
    
    payerse_payment_type        = fields.Char(string='Payment type')
    payerse_testmode            = fields.Boolean(string='Testmode')
    payerse_added_fee           = fields.Float(string='Added fee')
    payerse_paymentid           = fields.Char(string='Payment ID')
    
    @api.model
    def _payerse_form_get_tx_from_data(self, data):
        _logger.info('get txfrom data')
        reference = data[0].get('payer_merchant_reference_id', False)
        if reference:
            order = self.env['sale.order'].search([('name', '=', reference)])
            if len(order) != 1:
                error_msg = 'Payer.se: callback referenced non-existing sale order: %s' % reference
                _logger.warning(error_msg)
                raise ValidationError(error_msg)
            if not order[0].payment_tx_id:
                error_msg = 'Payer.se: callback referenced a sale order with no transaction: %s' % reference
                _logger.warning(error_msg)
                raise ValidationError(error_msg)
            return order[0].payment_tx_id
        else:
            error_msg = 'Payer.se: callback did not contain a sale order reference.'
            _logger.warning(error_msg)
            raise ValidationError(error_msg)
    
    @api.model
    def _payerse_form_get_invalid_parameters(self, tx, data):
        _logger.info('get invalid parameters')
        invalid_parameters = []
        post = data[0]
        url = data[1]
        ip = data[2]
        
        checksum = post.get('md5sum', None)
        url = url[0:url.rfind('&')]                 # Remove checksum
        url=urllib2.unquote(url).decode('utf8')     # Decode to UTF-8 from URI
        
        #~ msg = "\npost:\n"
        #~ for key in post:
            #~ msg += "\t%s:\t\t%s\n" % (key, post[key])
        #~ msg += "\nurl:\t%s\ndata:\t%s\nip:\t%s" % (url, callback_data, ip)
        #~ _logger.info(msg)
        
        expected = tx.acquirer_id._payerse_generate_checksum(url)
        testmode = post.get('payer_testmode', 'false') == 'true'
        if checksum:
            checksum = checksum.lower()
        else:
            invalid_parameters.append(('md5sum', 'None', 'a value'))
        if checksum and checksum != expected:
            invalid_parameters.append(('md5sum', checksum, expected))   # TODO: Remove logging of expected checksum.
        if not tx.acquirer_id.validate_ip(ip):
            invalid_parameters.append(('callback sender ip', ip, 'Not whitelisted'))
        if testmode != tx.payerse_testmode:
            invalid_parameters.append(('test_mode', testmode, tx.payerse_testmode))
        return invalid_parameters
    
    @api.model
    def _payerse_form_validate(self, tx, data):
        _logger.info('validate form')
        post = data[0]
        #order_id = post.get('order_id', False)                        #Original parameter added by merchants shop.
        payer_testmode = post.get('payer_testmode', False)	        #[true|false] – indicates test or live mode    
        payer_callback_type = post.get('payer_callback_type', False)    #[authorize|settle|store] – callback type
        payer_added_fee = post.get('payer_added_fee', False)	        #[when payer adds the fee for a specific payment type] - fee
        payer_payment_id = post.get('payer_payment_id', False)	        #[xxx@yyyyy – reference: max 64 characters long] - id
        #md5sum = post.get('md5sum', False)
                
        tx_data = {
            'payerse_payment_type': post.get('payer_payment_type', False),  #[invoice|card|sms|wywallet|bank|enter] – payment type
        }
        
        if payer_payment_id:
            tx_data['acquirer_reference'] = payer_payment_id
        if payer_testmode and payer_testmode == 'true':
            tx_data['payerse_testmode'] = True
        else:
            tx_data['payerse_testmode'] = False
        if payer_added_fee:
            tx_data['payerse_added_fee'] = payer_added_fee
        
        if not payer_callback_type:
            return False
        elif payer_callback_type == 'settle':
            _logger.info('Validated Payer.se payment for tx %s: set as done' % (tx.reference))
            tx_data.update(state='done', date_validate=fields.Datetime.now())
        elif payer_callback_type == 'auth':
            _logger.info('Received authorization for Payer.se payment %s: set as pending' % (tx.reference))
            tx_data.update(state='pending', state_message='Payment authorized by Payer.se')
        elif payer_callback_type == 'store':
            _logger.info('Received back to store callback from Payer.se payment %s' % (tx.reference))
            return True
        else:
            error = 'Received unrecognized status for Payper.se payment %s: %s, set as error' % (tx.reference, payer_callback_type)
            _logger.info(error)
            tx_data.update(state='error', state_message=error)
        return tx.write(tx_data)
    
    @api.model
    def payerse_create(self, values):
        #~ msg = "\n"
        #~ for key in values:
            #~ msg += "%s:\t\t%s\n" % (key, values[key])
        #~ _logger.info(msg)
        acquirer = self.env['payment.acquirer'].browse(values['acquirer_id'])
        values['payerse_testmode'] = True if acquirer.environment == 'test' else False
        
        return values

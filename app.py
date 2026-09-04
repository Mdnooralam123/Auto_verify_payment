"""
UPI Auto-Payment Verifier – Vercel + Supabase Edition
Colorful UI, glow animations, colored QR codes, all in one file.
"""

import os
import re
import time
import json
import logging
import imaplib
import email
import secrets
import qrcode
from io import BytesIO
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS

# Optional Supabase import (if installed)
try:
    from supabase import create_client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False
    create_client = None

# ============================================
# CONFIGURATION
# ============================================
CONFIG = {
    'UPI_ID': os.getenv('UPI_ID', '9304619487@fam'),
    'PAYEE_NAME': os.getenv('PAYEE_NAME', 'Md Nooralam'),
    'GMAIL_APP_PASSWORD': os.getenv('GMAIL_APP_PASSWORD', 'owjwtlotkfjnsftm'),
    'GMAIL_EMAIL': os.getenv('GMAIL_EMAIL', 'nkg166465@gmail.com'),
    'TIME_WINDOW_MINUTES': int(os.getenv('TIME_WINDOW_MINUTES', 5)),
    'ADMIN_API_KEY': os.getenv('ADMIN_API_KEY', 'khanbro786'),
    'MAX_EMAILS_CHECK': int(os.getenv('MAX_EMAILS_CHECK', 50)),
    'SUPABASE_URL': os.getenv('SUPABASE_URL'),
    'SUPABASE_KEY': os.getenv('SUPABASE_KEY'),
}

# ============================================
# LOGGING
# ============================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================
# FLASK APP
# ============================================
app = Flask(__name__)
CORS(app)

# ============================================
# DATABASE SETUP (Supabase or Fallback)
# ============================================
if CONFIG['SUPABASE_URL'] and CONFIG['SUPABASE_KEY'] and SUPABASE_AVAILABLE:
    supabase_client = create_client(CONFIG['SUPABASE_URL'], CONFIG['SUPABASE_KEY'])
    logger.info("Supabase client initialized.")
else:
    supabase_client = None
    logger.warning("Supabase not configured. Using in-memory fallback (data lost on restart).")

# Fallback storage (in-memory)
app.fallback_orders = {}
app.fallback_api_keys = {}
app.fallback_utrs = {}

def db_create_order(api_key, amount):
    order_id = f"Khan_{secrets.token_hex(4).upper()}"
    now = datetime.now().strftime('%d-%m-%Y %H:%M:%S')
    expires = (datetime.now() + timedelta(minutes=CONFIG['TIME_WINDOW_MINUTES'])).strftime('%d-%m-%Y %H:%M:%S')

    if supabase_client:
        try:
            data = {
                'order_id': order_id,
                'api_key': api_key,
                'amount': amount,
                'payable_amount': amount,
                'status': 'pending',
                'created_at': now,
                'expires_at': expires
            }
            supabase_client.table('orders').insert(data).execute()
            return order_id
        except Exception as e:
            logger.error(f"Supabase insert error: {e}")
            # Fallback to in-memory
            app.fallback_orders[order_id] = {
                'order_id': order_id,
                'api_key': api_key,
                'amount': amount,
                'payable_amount': amount,
                'status': 'pending',
                'utr': None,
                'transaction_id': None,
                'sender_name': None,
                'payment_time': None,
                'created_at': now,
                'expires_at': expires,
                'verified_at': None
            }
            return order_id
    else:
        app.fallback_orders[order_id] = {
            'order_id': order_id,
            'api_key': api_key,
            'amount': amount,
            'payable_amount': amount,
            'status': 'pending',
            'utr': None,
            'transaction_id': None,
            'sender_name': None,
            'payment_time': None,
            'created_at': now,
            'expires_at': expires,
            'verified_at': None
        }
        return order_id

def db_get_order(order_id):
    if supabase_client:
        try:
            result = supabase_client.table('orders').select('*').eq('order_id', order_id).execute()
            if result.data:
                return result.data[0]
            # Check fallback
            return app.fallback_orders.get(order_id)
        except Exception as e:
            logger.error(f"Supabase get error: {e}")
            return app.fallback_orders.get(order_id)
    else:
        return app.fallback_orders.get(order_id)

def db_update_order(order_id, **kwargs):
    if supabase_client:
        try:
            supabase_client.table('orders').update(kwargs).eq('order_id', order_id).execute()
        except Exception as e:
            logger.error(f"Supabase update error: {e}")
            if order_id in app.fallback_orders:
                app.fallback_orders[order_id].update(kwargs)
    else:
        if order_id in app.fallback_orders:
            app.fallback_orders[order_id].update(kwargs)

def db_create_api_key(name, expiry_hours=24):
    api_key = f"fam_{secrets.token_hex(20)}"
    now = datetime.now().isoformat()
    expires = (datetime.now() + timedelta(hours=expiry_hours)).isoformat()
    if supabase_client:
        try:
            data = {'api_key': api_key, 'name': name, 'created_at': now, 'expires_at': expires, 'is_active': 1}
            supabase_client.table('api_keys').insert(data).execute()
            return api_key
        except Exception as e:
            logger.error(f"Supabase insert api_key error: {e}")
            app.fallback_api_keys[api_key] = data
            return api_key
    else:
        app.fallback_api_keys[api_key] = {'api_key': api_key, 'name': name, 'created_at': now, 'expires_at': expires, 'is_active': 1}
        return api_key

def db_validate_api_key(api_key):
    if supabase_client:
        try:
            result = supabase_client.table('api_keys').select('*').eq('api_key', api_key).eq('is_active', 1).execute()
            if result.data:
                key = result.data[0]
                if datetime.now().isoformat() < key['expires_at']:
                    return key
            return None
        except Exception as e:
            logger.error(f"Supabase validate error: {e}")
            key = app.fallback_api_keys.get(api_key)
            if key and key['is_active'] == 1 and datetime.now().isoformat() < key['expires_at']:
                return key
            return None
    else:
        key = app.fallback_api_keys.get(api_key)
        if key and key['is_active'] == 1 and datetime.now().isoformat() < key['expires_at']:
            return key
        return None

def db_is_utr_verified(utr):
    if supabase_client:
        try:
            result = supabase_client.table('verified_utrs').select('*').eq('utr', utr).execute()
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Supabase verify utr error: {e}")
            return utr in app.fallback_utrs
    else:
        return utr in app.fallback_utrs

def db_mark_utr_verified(utr, order_id):
    if supabase_client:
        try:
            data = {'utr': utr, 'order_id': order_id, 'verified_at': datetime.now().isoformat()}
            supabase_client.table('verified_utrs').insert(data).execute()
        except Exception as e:
            logger.error(f"Supabase mark utr error: {e}")
            app.fallback_utrs[utr] = {'order_id': order_id, 'verified_at': datetime.now().isoformat()}
    else:
        app.fallback_utrs[utr] = {'order_id': order_id, 'verified_at': datetime.now().isoformat()}

def db_get_pending_orders():
    if supabase_client:
        try:
            result = supabase_client.table('orders').select('*').eq('status', 'pending').execute()
            return result.data
        except Exception as e:
            logger.error(f"Supabase get pending error: {e}")
            return [o for o in app.fallback_orders.values() if o['status'] == 'pending']
    else:
        return [o for o in app.fallback_orders.values() if o['status'] == 'pending']

def db_update_api_key(api_key, **kwargs):
    if supabase_client:
        try:
            supabase_client.table('api_keys').update(kwargs).eq('api_key', api_key).execute()
        except Exception as e:
            logger.error(f"Supabase update api_key error: {e}")
            if api_key in app.fallback_api_keys:
                app.fallback_api_keys[api_key].update(kwargs)
    else:
        if api_key in app.fallback_api_keys:
            app.fallback_api_keys[api_key].update(kwargs)

# ============================================
# GMAIL VERIFICATION (on-demand)
# ============================================
def connect_imap():
    if not CONFIG['GMAIL_EMAIL'] or not CONFIG['GMAIL_APP_PASSWORD']:
        raise Exception("Gmail credentials not configured")
    mail = imaplib.IMAP4_SSL('imap.gmail.com')
    mail.login(CONFIG['GMAIL_EMAIL'], CONFIG['GMAIL_APP_PASSWORD'])
    mail.select('INBOX')
    return mail

def get_email_body(mail, msg_id):
    try:
        result, data = mail.fetch(msg_id, '(RFC822)')
        if result != 'OK':
            return ''
        raw = data[0][1]
        msg = email.message_from_bytes(raw)
        body = ''
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == 'text/plain' and 'attachment' not in str(part.get('Content-Disposition')):
                    try:
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
                    except:
                        continue
        else:
            try:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            except:
                pass
        return body
    except:
        return ''

def parse_payment_email(body):
    details = {'amount': None, 'utr': None, 'transaction_id': None, 'sender': None,
               'date': None, 'type': None, 'payment_datetime': None, 'time_diff_minutes': None}
    if 'successfully received' in body.lower():
        details['type'] = 'received'
    elif 'successfully paid' in body.lower():
        details['type'] = 'paid'
        return details

    patterns = [r'₹([0-9]+(\.[0-9]+)?)', r'Amount\s*[:]\s*₹([0-9]+(\.[0-9]+)?)']
    for p in patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            details['amount'] = float(m.group(1))
            break

    utr_patterns = [r'UTR\s*[:]\s*([0-9]+)', r'UTR\s*([0-9]+)']
    for p in utr_patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            details['utr'] = m.group(1)
            break

    tx_patterns = [r'Transaction ID\s*[:]\s*([A-Z0-9]+)', r'Txn\s*[:]\s*([A-Z0-9]+)']
    for p in tx_patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            details['transaction_id'] = m.group(1)
            break

    sender_match = re.search(r'from\s*([A-Za-z\s.]+)', body, re.IGNORECASE)
    if sender_match:
        details['sender'] = sender_match.group(1).strip()

    date_match = re.search(r'([0-9]{2}:[0-9]{2}\s*(AM|PM)\s*IST,\s*[0-9]{2}\s*[A-Za-z]+\s*[0-9]{4})', body, re.IGNORECASE)
    if date_match:
        details['date'] = date_match.group(1)
        try:
            time_str = date_match.group(1)
            time_part = re.search(r'([0-9]{2}:[0-9]{2})\s*(AM|PM)', time_str)
            if time_part:
                hour, minute = map(int, time_part.group(1).split(':'))
                ampm = time_part.group(2)
                if ampm == 'PM' and hour != 12:
                    hour += 12
                elif ampm == 'AM' and hour == 12:
                    hour = 0
                now = datetime.now()
                dt = datetime(now.year, now.month, now.day, hour, minute)
                if dt > now:
                    dt -= timedelta(days=1)
                details['payment_datetime'] = dt.isoformat()
                details['time_diff_minutes'] = round((now - dt).total_seconds() / 60, 1)
        except:
            pass
    return details

def search_gmail_payment(amount=None, utr=None, time_window=None):
    if time_window is None:
        time_window = CONFIG['TIME_WINDOW_MINUTES']
    mail = None
    try:
        mail = connect_imap()
        result, data = mail.search(None, 'ALL')
        if result != 'OK' or not data[0]:
            return None
        ids = data[0].split()
        recent_ids = ids[-CONFIG['MAX_EMAILS_CHECK']:]
        now = datetime.now()
        for msg_id in recent_ids:
            msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            body = get_email_body(mail, msg_id_str)
            if not body:
                continue
            details = parse_payment_email(body)
            if details.get('type') != 'received':
                continue
            if details.get('payment_datetime'):
                dt = datetime.fromisoformat(details['payment_datetime'])
                if (now - dt).total_seconds() / 60 > time_window:
                    continue
            elif details.get('time_diff_minutes') is not None and details['time_diff_minutes'] > time_window:
                continue
            else:
                result2, data2 = mail.fetch(msg_id, '(BODY.PEEK[HEADER.FIELDS (DATE)])')
                if result2 == 'OK':
                    header = data2[0][1].decode('utf-8', errors='ignore')
                    date_match = re.search(r'Date:\s*(.+)', header, re.IGNORECASE)
                    if date_match:
                        try:
                            email_date = email.utils.parsedate_to_datetime(date_match.group(1))
                            diff = (datetime.now(email_date.tzinfo) - email_date).total_seconds() / 60 if email_date.tzinfo else (datetime.now() - email_date).total_seconds() / 60
                            if diff > time_window:
                                continue
                        except:
                            pass
            if amount is not None and details.get('amount') and abs(details['amount'] - amount) < 0.01:
                if utr is not None:
                    if details.get('utr') == utr:
                        return details
                else:
                    return details
            elif utr is not None and details.get('utr') == utr:
                return details
        return None
    except Exception as e:
        logger.error(f"Gmail search error: {e}")
        return None
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()
            except:
                pass

# ============================================
# ADMIN ROUTES
# ============================================
ADMIN_KEY = CONFIG['ADMIN_API_KEY']

def admin_required():
    provided = request.args.get('admin_key')
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        provided = auth_header.split(' ')[1]
    return provided == ADMIN_KEY

@app.route('/apikey_generate', methods=['GET'])
def apikey_generate():
    if not admin_required():
        return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
    name = request.args.get('name')
    if not name:
        return jsonify({'status': 'error', 'message': 'name parameter required'}), 400
    hours = request.args.get('hours')
    days = request.args.get('days')
    expiry_hours = 24
    if hours:
        try: expiry_hours = int(hours)
        except: pass
    elif days:
        try: expiry_hours = int(days) * 24
        except: pass
    api_key = db_create_api_key(name, expiry_hours)
    return jsonify({
        'status': 'success',
        'api_key': api_key,
        'name': name,
        'expires_at': (datetime.now() + timedelta(hours=expiry_hours)).isoformat()
    })

@app.route('/admin_orders', methods=['GET'])
def admin_orders():
    if not admin_required():
        return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
    orders = db_get_pending_orders()
    return jsonify({'status': 'success', 'orders': orders})

@app.route('/admin_keys', methods=['GET'])
def admin_keys():
    if not admin_required():
        return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
    if supabase_client:
        try:
            result = supabase_client.table('api_keys').select('*').execute()
            return jsonify({'status': 'success', 'api_keys': result.data})
        except:
            return jsonify({'status': 'success', 'api_keys': list(app.fallback_api_keys.values())})
    else:
        return jsonify({'status': 'success', 'api_keys': list(app.fallback_api_keys.values())})

@app.route('/admin_revoke', methods=['GET'])
def admin_revoke():
    if not admin_required():
        return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
    api_key = request.args.get('api_key')
    if not api_key:
        return jsonify({'status': 'error', 'message': 'api_key parameter required'}), 400
    db_update_api_key(api_key, is_active=0)
    return jsonify({'status': 'success', 'message': f'API key {api_key} revoked'})

@app.route('/admin_verify', methods=['GET'])
def admin_verify():
    if not admin_required():
        return jsonify({'status': 'error', 'message': 'Invalid or missing admin_key'}), 401
    order_id = request.args.get('order_id')
    utr = request.args.get('utr')
    if not order_id or not utr:
        return jsonify({'status': 'error', 'message': 'order_id and utr parameters required'}), 400
    order = db_get_order(order_id)
    if not order:
        return jsonify({'status': 'error', 'message': 'Order not found'}), 404
    if order['status'] == 'verified':
        return jsonify({'status': 'error', 'message': 'Order already verified'}), 400
    if db_is_utr_verified(utr):
        return jsonify({'status': 'error', 'message': 'UTR already used'}), 400
    db_update_order(order_id, status='verified', utr=utr)
    db_mark_utr_verified(utr, order_id)
    return jsonify({'status': 'success', 'message': 'Order verified manually'})

# ============================================
# PUBLIC API
# ============================================
@app.route('/api/qr.php', methods=['GET'])
def api_qr():
    api_key = request.args.get('api_key')
    amount = request.args.get('amount')
    if not api_key or not amount:
        return jsonify({'status': 'error', 'message': 'api_key and amount required'}), 400
    key_info = db_validate_api_key(api_key)
    if not key_info:
        return jsonify({'status': 'error', 'message': 'Invalid or expired API key'}), 401
    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError
    except:
        return jsonify({'status': 'error', 'message': 'Invalid amount'}), 400
    order_id = db_create_order(api_key, amount)
    order = db_get_order(order_id)
    upi_intent = f"upi://pay?pa={CONFIG['UPI_ID']}&pn=FamPay&tr={order_id}&tn=Payment+for+Order+{order_id}&am={amount}&cu=INR"
    base_url = request.url_root.rstrip('/')
    qr_url = f"{base_url}/api/qr-image.php?order_id={order_id}"
    checkout_url = f"{base_url}/pay.php?order_id={order_id}"
    return jsonify({
        'status': 'success',
        'data': {
            'order_id': order_id,
            'qr_url': qr_url,
            'checkout_url': checkout_url,
            'upi_id': CONFIG['UPI_ID'],
            'amount': str(amount),
            'payable_amount': str(amount),
            'upi_intent': upi_intent,
            'created_at_ist': order['created_at'],
            'expires_at_ist': order['expires_at']
        }
    })

@app.route('/api/verify-order.php', methods=['GET'])
def api_verify_order():
    api_key = request.args.get('api_key')
    order_id = request.args.get('order_id')
    if not api_key or not order_id:
        return jsonify({'status': 'error', 'message': 'api_key and order_id required'}), 400
    if not db_validate_api_key(api_key):
        return jsonify({'status': 'error', 'message': 'Invalid API key'}), 401
    order = db_get_order(order_id)
    if not order:
        return jsonify({'status': 'error', 'message': 'Order not found'}), 404

    now = datetime.now()
    expires = datetime.strptime(order['expires_at'], '%d-%m-%Y %H:%M:%S')
    if now > expires and order['status'] == 'pending':
        db_update_order(order_id, status='expired')
        order = db_get_order(order_id)

    if order['status'] == 'pending':
        payment = search_gmail_payment(amount=order['amount'])
        if payment:
            utr = payment.get('utr')
            if utr and not db_is_utr_verified(utr):
                db_update_order(order_id, status='verified', utr=utr,
                             transaction_id=payment.get('transaction_id'),
                             sender_name=payment.get('sender'),
                             payment_time=payment.get('date'))
                db_mark_utr_verified(utr, order_id)
                order = db_get_order(order_id)

    return jsonify({
        'status': 'success',
        'data': {
            'order_id': order['order_id'],
            'status': order['status'],
            'amount': order['amount'],
            'payable_amount': order['payable_amount'],
            'utr': order.get('utr'),
            'transaction_id': order.get('transaction_id'),
            'sender_name': order.get('sender_name'),
            'payment_time_ist': order.get('payment_time')
        }
    })

@app.route('/api/qr-image.php', methods=['GET'])
def qr_image():
    order_id = request.args.get('order_id')
    if not order_id:
        return jsonify({'status': 'error', 'message': 'order_id required'}), 400
    order = db_get_order(order_id)
    if not order:
        return jsonify({'status': 'error', 'message': 'Order not found'}), 404
    upi_intent = f"upi://pay?pa={CONFIG['UPI_ID']}&pn=FamPay&tr={order_id}&tn=Payment+for+Order+{order_id}&am={order['amount']}&cu=INR"
    # Colored QR Code
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(upi_intent)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#8b5cf6", back_color="#0a0a1a")
    img_io = BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    return send_file(img_io, mimetype='image/png')

# ============================================
# PAYMENT PAGE (Colorful UI)
# ============================================
@app.route('/pay.php', methods=['GET'])
def pay_page():
    order_id = request.args.get('order_id')
    if not order_id:
        return "Order ID missing", 400
    order = db_get_order(order_id)
    if not order:
        return "Order not found", 404

    base_url = request.url_root.rstrip('/')
    qr_url = f"{base_url}/api/qr-image.php?order_id={order_id}"
    verify_url = f"{base_url}/api/verify-order.php?api_key={order['api_key']}&order_id={order_id}"
    amount = order['amount']
    expires_at = order['expires_at']
    merchant = CONFIG['PAYEE_NAME']
    status = order['status']

    if status == 'verified':
        return render_template_string(SUCCESS_PAGE,
            order_id=order_id,
            utr=order.get('utr', 'N/A'),
            amount=amount,
            merchant=merchant,
            payment_time=order.get('payment_time', ''),
            sender=order.get('sender_name', '')
        )

    now = datetime.now()
    expires_dt = datetime.strptime(expires_at, '%d-%m-%Y %H:%M:%S')
    if now > expires_dt:
        if order['status'] == 'pending':
            db_update_order(order_id, status='expired')
        return render_template_string(EXPIRED_PAGE,
            order_id=order_id,
            amount=amount,
            merchant=merchant,
            expires_at=expires_at
        )

    return render_template_string(PAYMENT_PAGE_TEMPLATE,
        amount=amount,
        qr_url=qr_url,
        order_id=order_id,
        merchant=merchant,
        expires_at=expires_at,
        verify_url=verify_url
    )

# ============================================
# HTML TEMPLATES (Colorful UI with Glow)
# ============================================
SUCCESS_PAGE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Payment Successful 🎉</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a1a;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            overflow: hidden;
            margin: 0;
        }
        .glow-bg {
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: radial-gradient(circle at 30% 40%, #1a0533 0%, #0a0a1a 70%, #000 100%);
            z-index: 0;
        }
        .glow-ring {
            position: absolute;
            border-radius: 50%;
            filter: blur(80px);
            animation: glowPulse 3s ease-in-out infinite alternate;
        }
        .ring1 { width: 500px; height: 500px; top: 0%; left: 10%; background: #8b5cf6; opacity: 0.2; }
        .ring2 { width: 600px; height: 600px; bottom: 10%; right: 5%; background: #ec4899; opacity: 0.15; }
        .ring3 { width: 400px; height: 400px; top: 50%; left: 50%; background: #06b6d4; opacity: 0.12; }
        .ring4 { width: 300px; height: 300px; top: 20%; right: 20%; background: #f59e0b; opacity: 0.1; }
        @keyframes glowPulse {
            0% { transform: scale(1) rotate(0deg); opacity: 0.3; }
            100% { transform: scale(1.6) rotate(180deg); opacity: 0.7; }
        }
        .success-container {
            position: relative;
            z-index: 2;
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(30px);
            border-radius: 32px;
            padding: 50px 40px;
            max-width: 460px;
            width: 100%;
            text-align: center;
            box-shadow: 0 30px 80px rgba(0,0,0,0.6), 0 0 60px rgba(139,92,246,0.1);
            border: 1px solid rgba(255,255,255,0.08);
            animation: popIn 0.7s cubic-bezier(0.34, 1.56, 0.64, 1);
        }
        @keyframes popIn {
            0% { transform: scale(0.8) rotate(-5deg); opacity: 0; }
            100% { transform: scale(1) rotate(0); opacity: 1; }
        }
        .checkmark {
            width: 100px;
            height: 100px;
            background: linear-gradient(135deg, #34d399, #10b981, #059669);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 20px;
            box-shadow: 0 0 60px rgba(52, 211, 153, 0.5), 0 0 120px rgba(52, 211, 153, 0.2);
            animation: bounceIn 0.8s ease;
        }
        .checkmark svg {
            width: 60px;
            height: 60px;
            fill: none;
            stroke: white;
            stroke-width: 4;
            stroke-linecap: round;
            stroke-linejoin: round;
            stroke-dasharray: 60;
            stroke-dashoffset: 60;
            animation: drawCheck 0.5s ease forwards 0.3s;
        }
        @keyframes bounceIn {
            0% { transform: scale(0); }
            50% { transform: scale(1.2); }
            70% { transform: scale(0.9); }
            100% { transform: scale(1); }
        }
        @keyframes drawCheck {
            100% { stroke-dashoffset: 0; }
        }
        h1 {
            font-size: 28px;
            background: linear-gradient(135deg, #34d399, #60a5fa, #a78bfa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin: 10px 0 6px;
            font-weight: 700;
        }
        .sub { color: #94a3b8; font-size: 16px; margin-bottom: 24px; }
        .detail-box {
            background: rgba(255,255,255,0.05);
            border-radius: 16px;
            padding: 20px;
            text-align: left;
            margin: 20px 0;
            border: 1px solid rgba(255,255,255,0.06);
        }
        .detail-row {
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            font-size: 14px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .detail-row:last-child { border: none; }
        .detail-row .label { color: #94a3b8; }
        .detail-row .value { font-weight: 500; color: #e2e8f0; }
        .btn {
            display: inline-block;
            background: linear-gradient(135deg, #8b5cf6, #6366f1, #4f46e5);
            color: white;
            padding: 12px 36px;
            border-radius: 40px;
            text-decoration: none;
            font-weight: 600;
            transition: transform 0.2s, box-shadow 0.2s;
            box-shadow: 0 4px 30px rgba(99,102,241,0.4);
        }
        .btn:hover { transform: scale(1.05); box-shadow: 0 6px 40px rgba(99,102,241,0.6); }
        .confetti-container {
            position: fixed;
            top: 0; left: 0;
            width: 100%; height: 100%;
            pointer-events: none;
            z-index: 1;
            overflow: hidden;
        }
        .confetti {
            position: absolute;
            width: 10px; height: 10px;
            opacity: 0.9;
            animation: confettiFall linear forwards;
        }
        @keyframes confettiFall {
            0% { transform: translateY(-10px) rotate(0deg) scale(1); opacity: 1; }
            100% { transform: translateY(110vh) rotate(720deg) scale(0.5); opacity: 0; }
        }
    </style>
</head>
<body>
    <div class="glow-bg">
        <div class="glow-ring ring1"></div>
        <div class="glow-ring ring2"></div>
        <div class="glow-ring ring3"></div>
        <div class="glow-ring ring4"></div>
    </div>
    <div class="confetti-container" id="confetti"></div>
    <div class="success-container">
        <div class="checkmark">
            <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
        </div>
        <h1>Payment Successful!</h1>
        <p class="sub">Your order has been verified instantly</p>
        <div class="detail-box">
            <div class="detail-row"><span class="label">Order ID</span><span class="value">{{ order_id }}</span></div>
            <div class="detail-row"><span class="label">Amount</span><span class="value">₹ {{ amount }}</span></div>
            <div class="detail-row"><span class="label">UTR</span><span class="value">{{ utr }}</span></div>
            <div class="detail-row"><span class="label">Payment Time</span><span class="value">{{ payment_time }}</span></div>
            <div class="detail-row"><span class="label">Sender</span><span class="value">{{ sender }}</span></div>
        </div>
        <a href="/" class="btn">Done</a>
    </div>
    <script>
        (function() {
            const container = document.getElementById('confetti');
            const colors = ['#ff6b6b', '#fbbf24', '#34d399', '#60a5fa', '#a78bfa', '#f472b6', '#fb923c', '#22d3ee', '#f43f5e', '#8b5cf6'];
            for (let i = 0; i < 120; i++) {
                const el = document.createElement('div');
                el.className = 'confetti';
                el.style.left = Math.random() * 100 + '%';
                el.style.width = (Math.random() * 10 + 5) + 'px';
                el.style.height = (Math.random() * 10 + 5) + 'px';
                el.style.background = colors[Math.floor(Math.random() * colors.length)];
                el.style.borderRadius = Math.random() > 0.5 ? '50%' : '2px';
                el.style.animationDuration = (Math.random() * 2.5 + 1.5) + 's';
                el.style.animationDelay = (Math.random() * 2.5) + 's';
                container.appendChild(el);
            }
        })();
    </script>
</body>
</html>
'''

EXPIRED_PAGE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Order Expired ⏰</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a1a;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
        }
        .card {
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(20px);
            border-radius: 32px;
            padding: 50px 40px;
            max-width: 420px;
            width: 100%;
            text-align: center;
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
            border: 1px solid rgba(255,255,255,0.08);
        }
        .icon { font-size: 64px; margin-bottom: 16px; }
        h1 { color: #f87171; font-size: 26px; margin-bottom: 8px; }
        .sub { color: #94a3b8; font-size: 16px; margin-bottom: 20px; }
        .detail { background: rgba(255,255,255,0.06); border-radius: 12px; padding: 16px; margin: 16px 0; border: 1px solid rgba(255,255,255,0.06); }
        .detail .row { display: flex; justify-content: space-between; padding: 6px 0; font-size: 15px; color: #e2e8f0; }
        .row .label { color: #94a3b8; }
        .row .value { font-weight: 500; }
        .btn {
            display: inline-block;
            background: linear-gradient(135deg, #8b5cf6, #6366f1);
            color: white;
            padding: 12px 32px;
            border-radius: 40px;
            text-decoration: none;
            font-weight: 500;
            transition: transform 0.2s;
        }
        .btn:hover { transform: scale(1.03); }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">⏰</div>
        <h1>Order Expired</h1>
        <p class="sub">This payment session has expired. Please create a new order.</p>
        <div class="detail">
            <div class="row"><span class="label">Order ID</span><span class="value">{{ order_id }}</span></div>
            <div class="row"><span class="label">Amount</span><span class="value">₹ {{ amount }}</span></div>
            <div class="row"><span class="label">Expired at</span><span class="value">{{ expires_at }}</span></div>
        </div>
        <a href="/" class="btn">Go Home</a>
    </div>
</body>
</html>
'''

PAYMENT_PAGE_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pay ₹{amount} - FamGateway</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a1a;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 20px;
        }}
        .glow-bg {{
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: radial-gradient(circle at 30% 40%, #1a0533 0%, #0a0a1a 70%, #000 100%);
            z-index: 0;
        }}
        .glow-ring {{
            position: absolute;
            border-radius: 50%;
            filter: blur(80px);
            animation: glowPulse 4s ease-in-out infinite alternate;
        }}
        .ring1 {{ width: 400px; height: 400px; top: 0%; left: 10%; background: #8b5cf6; opacity: 0.15; }}
        .ring2 {{ width: 500px; height: 500px; bottom: 10%; right: 5%; background: #ec4899; opacity: 0.1; }}
        .ring3 {{ width: 300px; height: 300px; top: 50%; left: 50%; background: #06b6d4; opacity: 0.08; }}
        @keyframes glowPulse {{
            0% {{ transform: scale(1) rotate(0deg); opacity: 0.2; }}
            100% {{ transform: scale(1.5) rotate(180deg); opacity: 0.5; }}
        }}
        .card {{
            position: relative;
            z-index: 2;
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(30px);
            border-radius: 28px;
            padding: 30px;
            max-width: 420px;
            width: 100%;
            border: 1px solid rgba(255,255,255,0.08);
            box-shadow: 0 30px 80px rgba(0,0,0,0.5);
        }}
        .header {{ text-align: center; margin-bottom: 24px; }}
        .header .brand {{ font-size: 22px; font-weight: 700; color: #f1f5f9; letter-spacing: -0.5px; }}
        .header .brand span {{ background: linear-gradient(135deg, #8b5cf6, #ec4899, #f59e0b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .order-total {{
            background: linear-gradient(135deg, rgba(139,92,246,0.15), rgba(236,72,153,0.1));
            border-radius: 16px;
            padding: 20px;
            text-align: center;
            margin-bottom: 24px;
            border: 1px solid rgba(139,92,246,0.15);
        }}
        .order-total .label {{ font-size: 13px; color: #94a3b8; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }}
        .order-total .amount {{ font-size: 38px; font-weight: 700; background: linear-gradient(135deg, #f1f5f9, #94a3b8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-top: 4px; }}
        .order-total .currency {{ font-size: 20px; color: #64748b; -webkit-text-fill-color: #64748b; }}
        .qr-section {{ text-align: center; margin: 20px 0 16px; }}
        .qr-section img {{ width: 200px; height: 200px; border: 2px solid rgba(139,92,246,0.2); border-radius: 16px; padding: 8px; background: white; box-shadow: 0 0 40px rgba(139,92,246,0.15); }}
        .qr-section .sub {{ font-size: 14px; color: #94a3b8; margin-top: 8px; }}
        .qr-section .save-btn {{
            display: inline-block;
            margin-top: 10px;
            background: rgba(255,255,255,0.08);
            color: #e2e8f0;
            padding: 8px 20px;
            border-radius: 30px;
            font-size: 14px;
            font-weight: 500;
            text-decoration: none;
            transition: 0.2s;
            border: 1px solid rgba(255,255,255,0.05);
        }}
        .qr-section .save-btn:hover {{ background: rgba(255,255,255,0.15); }}
        .details {{ margin: 20px 0; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 20px; }}
        .detail-row {{ display: flex; justify-content: space-between; padding: 8px 0; font-size: 15px; }}
        .detail-row .label {{ color: #94a3b8; }}
        .detail-row .value {{ font-weight: 500; color: #e2e8f0; }}
        .status {{
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 16px;
            text-align: center;
            margin: 16px 0;
            font-size: 15px;
            color: #8b5cf6;
            font-weight: 500;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            border: 1px solid rgba(139,92,246,0.15);
        }}
        .status .spinner {{
            display: inline-block;
            width: 18px;
            height: 18px;
            border: 3px solid rgba(139,92,246,0.2);
            border-top: 3px solid #8b5cf6;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        .status.verified {{ background: rgba(52,211,153,0.12); color: #34d399; border-color: rgba(52,211,153,0.2); }}
        .status.verified .spinner {{ display: none; }}
        .status.expired {{ background: rgba(248,113,113,0.12); color: #f87171; border-color: rgba(248,113,113,0.2); }}
        .status.expired .spinner {{ display: none; }}
        .check-link {{ text-align: center; margin-top: 12px; font-size: 14px; }}
        .check-link a {{ color: #8b5cf6; text-decoration: none; font-weight: 500; transition: 0.2s; }}
        .check-link a:hover {{ text-decoration: underline; color: #a78bfa; }}
        .footer {{ text-align: center; margin-top: 20px; font-size: 13px; color: #475569; }}
        .timer-warning {{ color: #f87171; font-weight: 600; }}
        .glow-text {{
            animation: textGlow 2s ease-in-out infinite alternate;
        }}
        @keyframes textGlow {{
            0% {{ text-shadow: 0 0 20px rgba(139,92,246,0.2); }}
            100% {{ text-shadow: 0 0 40px rgba(139,92,246,0.4); }}
        }}
    </style>
</head>
<body>
    <div class="glow-bg">
        <div class="glow-ring ring1"></div>
        <div class="glow-ring ring2"></div>
        <div class="glow-ring ring3"></div>
    </div>
    <div class="card">
        <div class="header"><div class="brand">Fam<span>Gateway</span>™</div></div>
        <div class="order-total">
            <div class="label">ORDER TOTAL</div>
            <div class="amount">₹ {amount:.2f} <span class="currency">INR</span></div>
        </div>
        <div class="qr-section">
            <img id="qr-img" src="{qr_url}" alt="QR Code">
            <div class="sub">SCAN WITH ANY UPI APP</div>
            <a href="{qr_url}" download="qr_{order_id}.png" class="save-btn">🔗 Save QR</a>
        </div>
        <div class="details">
            <div class="detail-row"><span class="label">Merchant</span><span class="value">{merchant}</span></div>
            <div class="detail-row"><span class="label">Order ID</span><span class="value">{order_id}</span></div>
            <div class="detail-row">
                <span class="label">Expires In</span>
                <span class="value glow-text" id="timer">--:--</span>
            </div>
        </div>
        <div class="status" id="status">
            <span class="spinner"></span>
            <span id="status-text">Waiting for payment...</span>
        </div>
        <div class="check-link"><a href="{verify_url}" target="_blank">Check Status</a></div>
        <div class="footer">⚡ Auto‑verified instantly after UPI payment</div>
    </div>
    <script>
        const expiresStr = "{expires_at}";
        const [datePart, timePart] = expiresStr.split(' ');
        const [dd, mm, yyyy] = datePart.split('-');
        const [hh, min, sec] = timePart.split(':');
        const expires = new Date(yyyy, mm-1, dd, hh, min, sec).getTime();
        const timerEl = document.getElementById('timer');

        function updateTimer() {{
            const now = Date.now();
            let diff = expires - now;
            if (diff < 0) {{
                timerEl.textContent = 'Expired';
                timerEl.className = 'timer-warning glow-text';
                document.getElementById('status').className = 'status expired';
                document.getElementById('status-text').textContent = '⏰ Order expired';
                return;
            }}
            const mins = Math.floor(diff / 60000);
            const secs = Math.floor((diff % 60000) / 1000);
            timerEl.textContent = String(mins).padStart(2,'0') + ':' + String(secs).padStart(2,'0');
        }}
        updateTimer();
        setInterval(updateTimer, 1000);

        const statusEl = document.getElementById('status');
        const statusText = document.getElementById('status-text');
        const verifyUrl = "{verify_url}";

        function checkStatus() {{
            fetch(verifyUrl)
                .then(res => res.json())
                .then(data => {{
                    if (data.data && data.data.status === 'verified') {{
                        statusEl.className = 'status verified';
                        statusText.textContent = '✅ Payment Verified!';
                        setTimeout(() => location.reload(), 1500);
                    }} else if (data.data && data.data.status === 'expired') {{
                        statusEl.className = 'status expired';
                        statusText.textContent = '⏰ Order expired';
                        setTimeout(() => location.reload(), 1000);
                    }}
                }})
                .catch(() => {{}});
        }}
        setInterval(checkStatus, 3000);
        checkStatus();
    </script>
</body>
</html>
'''

# ============================================
# VERIFICATION ENDPOINTS
# ============================================
@app.route('/verify-fast', methods=['GET'])
def verify_fast():
    amount = request.args.get('amount')
    utr = request.args.get('utr')
    time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
    if not amount and not utr:
        return jsonify({'status': 'error', 'message': 'Provide amount or utr'}), 400
    try:
        if amount:
            amount = float(amount)
        time_window = int(time_window)
    except:
        return jsonify({'status': 'error', 'message': 'Invalid input'}), 400
    payment = search_gmail_payment(amount=amount, utr=utr, time_window=time_window)
    if payment:
        return jsonify({'status': 'success', 'message': '✅ Payment found!', 'data': payment})
    else:
        return jsonify({'status': 'not_found', 'message': f'❌ No matching payment found in last {time_window} minutes.'})

@app.route('/verify-by-utr', methods=['GET', 'POST'])
def verify_by_utr():
    if request.method == 'GET':
        utr = request.args.get('utr')
        time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
    else:
        data = request.get_json()
        utr = data.get('utr') if data else None
        time_window = data.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
    if not utr:
        return jsonify({'status': 'error', 'message': 'UTR required'}), 400
    try:
        time_window = int(time_window)
    except:
        return jsonify({'status': 'error', 'message': 'Invalid time_window'}), 400
    payment = search_gmail_payment(utr=utr, time_window=time_window)
    if payment:
        return jsonify({'status': 'success', 'message': '✅ Payment found by UTR', 'data': payment})
    else:
        return jsonify({'status': 'not_found', 'message': f'No payment with UTR {utr} in last {time_window} min'})

@app.route('/verify-last-payment', methods=['GET'])
def verify_last_payment():
    amount = request.args.get('amount')
    time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
    if not amount:
        return jsonify({'status': 'error', 'message': 'Amount required'}), 400
    try:
        amount = float(amount)
        time_window = int(time_window)
    except:
        return jsonify({'status': 'error', 'message': 'Invalid input'}), 400
    payment = search_gmail_payment(amount=amount, time_window=time_window)
    if payment:
        return jsonify({'status': 'success', 'message': '✅ Payment found', 'data': payment})
    else:
        return jsonify({'status': 'not_found', 'message': f'No payment of ₹{amount} in last {time_window} min'})

@app.route('/verify-payment', methods=['GET', 'POST'])
def verify_payment_legacy():
    if request.method == 'GET':
        amount = request.args.get('amount')
        time_window = request.args.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
    else:
        data = request.get_json()
        amount = data.get('amount') if data else None
        time_window = data.get('time_window', CONFIG['TIME_WINDOW_MINUTES'])
    if not amount:
        return jsonify({'status': 'error', 'message': 'Amount required'}), 400
    try:
        amount = float(amount)
        time_window = int(time_window)
    except:
        return jsonify({'status': 'error', 'message': 'Invalid input'}), 400
    payment = search_gmail_payment(amount=amount, time_window=time_window)
    if payment:
        return jsonify({'status': 'success', 'message': '✅ Payment verified', 'data': payment})
    else:
        return jsonify({'status': 'pending', 'message': f'⏳ No payment found in last {time_window} min'})

@app.route('/generate-qr', methods=['GET'])
def generate_qr_legacy():
    amount = request.args.get('amount')
    if not amount:
        return jsonify({'status': 'error', 'message': 'Amount required'}), 400
    try:
        amount = float(amount)
    except:
        return jsonify({'status': 'error', 'message': 'Invalid amount'}), 400
    upi_intent = f"upi://pay?pa={CONFIG['UPI_ID']}&pn=FamPay&am={amount}&cu=INR"
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(upi_intent)
    qr.make(fit=True)
    # Colored QR
    img = qr.make_image(fill_color="#8b5cf6", back_color="#0a0a1a")
    img_io = BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    return send_file(img_io, mimetype='image/png')

@app.route('/debug-emails', methods=['GET'])
def debug_emails():
    payment = search_gmail_payment()
    return jsonify({'status': 'debug', 'last_payment': payment})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

@app.route('/', methods=['GET'])
def index():
    base_url = request.url_root.rstrip('/')
    return jsonify({
        'name': 'UPI Auto-Payment Verifier',
        'version': '5.0.0',
        'description': 'Colorful payment verification with Supabase, glow animations, and colored QR codes.',
        'endpoints': {
            'public': {
                '/': 'GET - This documentation',
                '/health': 'GET - Health check',
                '/generate-qr': 'GET - Generate colored QR (e.g., ?amount=499)',
                '/verify-fast': 'GET - Instant verify by amount or UTR',
                '/verify-by-utr': 'GET/POST - Verify by UTR only',
                '/verify-last-payment': 'GET - One-shot check by amount',
                '/verify-payment': 'GET/POST - Legacy polling',
                '/api/qr.php': 'GET - Create order and get QR (api_key, amount)',
                '/api/verify-order.php': 'GET - Check order status (api_key, order_id)',
                '/api/qr-image.php': 'GET - Get colored QR image (order_id)',
                '/pay.php': 'GET - Payment page with glow UI (order_id)',
                '/debug-emails': 'GET - Debug'
            }
        },
        'examples': {
            'create_order': f'curl "{base_url}/api/qr.php?api_key=fam_YOUR_KEY&amount=499"',
            'verify_by_amount': f'curl "{base_url}/verify-fast?amount=1"',
            'payment_page': f'Open in browser: {base_url}/pay.php?order_id=Khan_YOUR_ID'
        }
    })

# ============================================
# VERCEL ENTRY POINT
# ============================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
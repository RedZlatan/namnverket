# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify, render_template_string, redirect, make_response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import sqlite3
import requests
import anthropic
import stripe
import uuid
import re
from dotenv import load_dotenv
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote
import os
import time
import math
import threading
import secrets
from datetime import datetime, timedelta
import dns.resolver as _dns_resolver

load_dotenv(os.path.expanduser('~/Desktop/namnge/nyklar/.env'))

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

PAKET = {
    'bas':      {'namn': 'Bas',      'tokens': 50,  'pris_ore': 1900, 'pris_text': '19 kr'},
    'standard': {'namn': 'Standard', 'tokens': 200, 'pris_ore': 4900, 'pris_text': '49 kr'},
    'pro':      {'namn': 'Pro',      'tokens': 500, 'pris_ore': 9900, 'pris_text': '99 kr'},
}

app = Flask(__name__)
app.json.ensure_ascii = False
DB = '../bolag.db'

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri='memory://',
)

@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({'error': 'För många anrop, vänta lite och försök igen.'}), 429

# --- Input validation helpers ---
_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')
_DOMAN_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9\-]*(\.[a-zA-Z0-9][a-zA-Z0-9\-]*)+$')
_HTML_RE  = re.compile(r'<[^>]+>')

def _valider_email(email):
    return bool(email and _EMAIL_RE.match(email.strip()))

def _valider_doman(doman):
    return bool(doman and len(doman) <= 253 and _DOMAN_RE.match(doman))

def _sanera_text(text, max_len=100):
    return _HTML_RE.sub('', (text or ''))[:max_len].strip()

def init_db():
    con = sqlite3.connect(DB)
    con.execute('''CREATE TABLE IF NOT EXISTS tokens (
        session_id TEXT PRIMARY KEY,
        tokens INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS betalningar (
        stripe_session_id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS sparade_namn (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT,
        namn TEXT,
        bransch TEXT,
        typ TEXT,
        skapad TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS "köpta_domäner" (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT,
        doman TEXT,
        köpdatum TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        förnyelsedatum TEXT,
        openprovider_id TEXT
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS sokningar (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        namn TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        hittade_ledigt BOOLEAN
    )''')
    con.execute('CREATE INDEX IF NOT EXISTS idx_sok_namn ON sokningar(namn)')
    con.execute('''CREATE TABLE IF NOT EXISTS pending_listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doman TEXT UNIQUE,
        pris INTEGER,
        saljare_email TEXT,
        beskrivning TEXT,
        verify_code TEXT,
        skapad TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS domanmarknaden (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doman TEXT UNIQUE,
        pris INTEGER,
        saljare_email TEXT,
        beskrivning TEXT,
        listad TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'aktiv'
    )''')
    con.execute('CREATE INDEX IF NOT EXISTS idx_dm_doman ON domanmarknaden(doman)')
    con.execute('CREATE INDEX IF NOT EXISTS idx_dm_status ON domanmarknaden(status)')
    con.execute('''CREATE TABLE IF NOT EXISTS provisioner (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doman TEXT,
        forsaljningspris INTEGER,
        provision INTEGER,
        kopare_email TEXT,
        saljare_email TEXT,
        stripe_session_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    con.commit()
    con.close()

def markera_betald(stripe_session_id):
    """Returnerar True om detta är en ny betalning, False om redan hanterad."""
    con = sqlite3.connect(DB)
    try:
        con.execute('INSERT INTO betalningar (stripe_session_id) VALUES (?)', (stripe_session_id,))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()

init_db()

def _logga_email(till, amne, text):
    """Stub — loggar email till konsolen tills SMTP/Mailjet konfigureras."""
    print(f'[EMAIL] till={till!r} ämne={amne!r}\n{text}', flush=True)

def _kolla_dns_verifiering(doman, verify_code):
    try:
        answers = _dns_resolver.resolve(doman, 'TXT')
        for rdata in answers:
            for txt_string in rdata.strings:
                if verify_code.encode() in txt_string:
                    return True
    except Exception as e:
        print(f'[DNS] {doman}: {e}', flush=True)
    return False

BELOPP_TOKENS = {1900: 50, 4900: 200, 9900: 500}

def get_session_id():
    email = request.cookies.get('nk_email', '').strip()
    if email:
        return email
    return request.cookies.get('sid') or str(uuid.uuid4())

def get_tokens(sid):
    con = sqlite3.connect(DB)
    row = con.execute('SELECT tokens FROM tokens WHERE session_id = ?', (sid,)).fetchone()
    con.close()
    return row[0] if row else 0

def deduct_tokens(sid, amount):
    con = sqlite3.connect(DB)
    con.execute(
        'INSERT INTO tokens (session_id, tokens) VALUES (?, 0) ON CONFLICT(session_id) DO NOTHING',
        (sid,)
    )
    con.execute(
        'UPDATE tokens SET tokens = tokens - ? WHERE session_id = ? AND tokens >= ?',
        (amount, sid, amount)
    )
    rows = con.execute('SELECT changes()').fetchone()[0]
    con.commit()
    con.close()
    return rows > 0

def add_tokens(sid, amount):
    con = sqlite3.connect(DB)
    con.execute(
        'INSERT INTO tokens (session_id, tokens) VALUES (?, ?) '
        'ON CONFLICT(session_id) DO UPDATE SET tokens = tokens + excluded.tokens',
        (sid, amount)
    )
    con.commit()
    con.close()

@app.route('/spara', methods=['POST'])
def spara_namn():
    body = request.get_json() or {}
    email = (body.get('email') or '').strip()
    namn = _sanera_text(body.get('namn') or '')
    bransch = _sanera_text(body.get('bransch') or '')
    typ = _sanera_text(body.get('typ') or '')
    if not namn:
        return jsonify({'ok': False, 'error': 'Namn saknas'})
    if not _valider_email(email):
        return jsonify({'ok': False, 'error': 'Ogiltig e-postadress'})
    print(f'[SPARA] namn={namn!r} email={email!r} bransch={bransch!r} typ={typ!r}', flush=True)
    con = sqlite3.connect(DB)
    try:
        con.execute(
            'INSERT INTO sparade_namn (email, namn, bransch, typ) VALUES (?, ?, ?, ?)',
            (email, namn, bransch, typ)
        )
        con.commit()
    except Exception as e:
        con.close()
        print(f'[SPARA] FEL: {e}', flush=True)
        return jsonify({'ok': False, 'error': 'Kunde inte spara, försök igen.'})
    con.close()
    return jsonify({'ok': True})

@app.route('/sparade')
def sparade_namn():
    sid = get_session_id()
    con = sqlite3.connect(DB)
    rows = con.execute(
        'SELECT namn, bransch, typ, skapad FROM sparade_namn WHERE email = ? ORDER BY skapad DESC',
        (sid,)
    ).fetchall()
    con.close()
    return jsonify({'namn': [{'namn': r[0], 'bransch': r[1], 'typ': r[2], 'skapad': r[3]} for r in rows]})

@app.route('/sparade/<path:namn>', methods=['DELETE'])
def ta_bort_sparat(namn):
    sid = get_session_id()
    con = sqlite3.connect(DB)
    con.execute('DELETE FROM sparade_namn WHERE email = ? AND namn = ?', (sid, namn))
    con.commit()
    con.close()
    return jsonify({'ok': True})

_op_token_cache = {'token': None, 'ts': 0}
_op_handle_cache = {'handle': 'RO917468-SE'}

def get_op_token():
    now = time.time()
    if _op_token_cache['token'] and now - _op_token_cache['ts'] < 47 * 3600:
        return _op_token_cache['token']
    username = os.environ.get('OPENPROVIDER_USER', '')
    print(f'[OP] Loggar in med username="{username}"', flush=True)
    payload = {
        'username': username,
        'password': os.environ.get('OPENPROVIDER_PASS', ''),
        'ip': '0.0.0.0',
    }
    r = requests.post(
        'https://api.openprovider.eu/v1beta/auth/login',
        json=payload,
        timeout=10
    )
    print(f'[OP] Auth svar HTTP {r.status_code}: {r.text[:200]}', flush=True)
    data = r.json()
    if data.get('code') != 0:
        raise Exception(data.get('desc', 'Auth misslyckades'))
    token = data['data']['token']
    _op_token_cache['token'] = token
    _op_token_cache['ts'] = now
    return token

def get_op_handle():
    if _op_handle_cache['handle']:
        return _op_handle_cache['handle']
    token = get_op_token()
    r = requests.get(
        'https://api.openprovider.eu/v1beta/customers',
        params={'limit': 1, 'email': os.environ.get('OPENPROVIDER_USER', '')},
        headers={'Authorization': f'Bearer {token}'},
        timeout=10
    )
    print(f'[OP] customers HTTP {r.status_code}: {r.text[:300]}', flush=True)
    data = r.json()
    if data.get('code') != 0:
        raise Exception(data.get('desc', 'Kunde inte hämta handle'))
    results = (data.get('data') or {}).get('results') or []
    if not results:
        raise Exception('Ingen kund hittad i Openprovider')
    handle = results[0].get('handle', '')
    print(f'[OP] handle="{handle}"', flush=True)
    _op_handle_cache['handle'] = handle
    return handle

_USD_SEK = 10.5  # approximate exchange rate

def hämta_pris(doman):
    """Returnerar (grossistpris_sek, kundpris_kr, valuta_orig). Kastar Exception vid fel."""
    namn, ext = doman.split('.', 1)
    token = get_op_token()
    r = requests.post(
        'https://api.openprovider.eu/v1beta/domains/check',
        headers={'Authorization': f'Bearer {token}'},
        json={'domains': [{'name': namn, 'extension': ext}], 'with_price': True},
        timeout=10
    )
    data = r.json()
    print(f'[PRIS] doman={doman} råsvar={data}', flush=True)
    if data.get('code') != 0:
        raise Exception(data.get('desc', 'API-fel'))
    results = (data.get('data') or {}).get('results') or []
    if not results:
        raise Exception('Pris ej tillgängligt')
    price_obj = results[0].get('price') or {}
    product = price_obj.get('product') or {}
    reseller = price_obj.get('reseller') or {}
    pris = product.get('price') or reseller.get('price')
    valuta = product.get('currency') or reseller.get('currency', 'SEK')
    print(f'[PRIS] doman={doman} price_obj={price_obj} pris={pris} valuta={valuta}', flush=True)
    if pris is None:
        raise Exception('Pris saknas i svar')
    grossistpris = float(pris)
    # Openprovider returnerar USD för internationella TLDs — konvertera till SEK
    if valuta == 'USD':
        grossistpris = grossistpris * _USD_SEK
    kundpris = math.ceil(grossistpris * 1.67)
    print(f'[PRIS] doman={doman} grossist_sek={grossistpris:.2f} kundpris={kundpris} valuta_orig={valuta}', flush=True)
    return grossistpris, kundpris, valuta

_BOLAGSFORM_NAMN = {
    'AB-ORGFO': 'Aktiebolag', 'E-ORGFO': 'Enskild firma',
    'HB-ORGFO': 'Handelsbolag', 'BRF-ORGFO': 'Bostadsrättsförening',
    'EK-ORGFO': 'Ekonomisk förening', 'FL-ORGFO': 'Filial',
    'KB-ORGFO': 'Kommanditbolag', 'I-ORGFO': 'Ideell förening',
}

_TRENDER_CACHE  = {'data': None, 'ts': 0}
_CACHE_TTL      = 3600 * 6  # 6 h

def _fmt_sek(n):
    return f"{int(n):,}".replace(",", " ")  # non-breaking space as thousands sep

_TOP_DOMANER = [
    {'doman': 'AI.com',              'usd': 70_000_000,  'sek': _fmt_sek(70_000_000  * 10.5), 'ar': 2025},
    {'doman': 'Voice.com',           'usd': 30_000_000,  'sek': _fmt_sek(30_000_000  * 10.5), 'ar': 2019},
    {'doman': '360.com',             'usd': 17_000_000,  'sek': _fmt_sek(17_000_000  * 10.5), 'ar': 2015},
    {'doman': 'Chat.com',            'usd': 15_500_000,  'sek': _fmt_sek(15_500_000  * 10.5), 'ar': 2023},
    {'doman': 'Porno.com',           'usd':  8_888_888,  'sek': _fmt_sek( 8_888_888  * 10.5), 'ar': 2015},
    {'doman': 'Gold.com',            'usd':  8_500_000,  'sek': _fmt_sek( 8_500_000  * 10.5), 'ar': 2024},
    {'doman': 'HealthInsurance.com', 'usd':  8_130_000,  'sek': _fmt_sek( 8_130_000  * 10.5), 'ar': 2019},
    {'doman': 'Beer.com',            'usd':  7_000_000,  'sek': _fmt_sek( 7_000_000  * 10.5), 'ar': 2004},
    {'doman': 'Z.com',               'usd':  6_800_000,  'sek': _fmt_sek( 6_800_000  * 10.5), 'ar': 2014},
    {'doman': 'Slots.com',           'usd':  5_500_000,  'sek': _fmt_sek( 5_500_000  * 10.5), 'ar': 2010},
]

def _hämta_trender():
    now = time.time()
    if _TRENDER_CACHE['data'] is not None and now - _TRENDER_CACHE['ts'] < _CACHE_TTL:
        return _TRENDER_CACHE['data']
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl='sv-SE', tz=60, timeout=(10, 30), retries=1)
        kw = ['e-handel', 'konsultbolag', 'restaurang starta', 'tech startup', 'frisör eget']
        pt.build_payload(kw, geo='SE', timeframe='now 7-d')
        df = pt.interest_over_time()
        if df.empty:
            result = []
        else:
            df = df.drop(columns=['isPartial'], errors='ignore')
            mid = len(df) // 2
            första = df.iloc[:mid].mean()
            sista  = df.iloc[mid:].mean()
            result = []
            for k in kw:
                v_ny  = float(sista.get(k, 0))
                v_gml = float(första.get(k, 0))
                pil   = '↑' if v_ny > v_gml + 2 else ('↓' if v_ny < v_gml - 2 else '→')
                result.append({'namn': k, 'varde': int(v_ny), 'pil': pil})
            result.sort(key=lambda x: x['varde'], reverse=True)
        _TRENDER_CACHE['data'] = result
        _TRENDER_CACHE['ts']   = now
        return result
    except Exception as e:
        print(f'[TRENDER] pytrends fel: {e}', flush=True)
        _TRENDER_CACHE['data'] = []
        _TRENDER_CACHE['ts']   = now
        return []

def _mest_sokta(limit=10):
    try:
        con = sqlite3.connect(DB)
        rows = con.execute(
            '''SELECT namn, COUNT(*) as antal FROM sokningar
               WHERE timestamp > datetime('now', '-7 days')
               GROUP BY namn ORDER BY antal DESC LIMIT ?''', (limit,)
        ).fetchall()
        con.close()
        return [{'namn': r[0], 'antal': r[1]} for r in rows]
    except Exception:
        return []

def _nya_bolag(limit=6):
    try:
        con = sqlite3.connect(DB)
        rows = con.execute(
            '''SELECT bolagsform, COUNT(*) as antal FROM bolag
               WHERE reg_datum > date('now', '-30 days')
               GROUP BY bolagsform ORDER BY antal DESC LIMIT ?''', (limit,)
        ).fetchall()
        con.close()
        return [{'form': _BOLAGSFORM_NAMN.get(r[0], r[0]), 'antal': r[1]} for r in rows]
    except Exception:
        return []

# Värm upp trender-cachen i bakgrunden vid start
threading.Thread(target=_hämta_trender, daemon=True).start()

HTML = '''
<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%230a0a0a' rx='6'/><text x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' fill='white' font-family='Inter,sans-serif' font-weight='500' font-size='18'>N</text></svg>">
    <title>Namnverket — Hitta ett namn som faktiskt är ledigt</title>
    <meta name="description" content="Kolla om ett företagsnamn är ledigt hos Bolagsverket, domäner och varumärken i ett slag. Gratis namnkoll för svenska företag.">
    <meta name="keywords" content="företagsnamn, bolagsnamn, domän ledig, namnkoll, registrera företag Sverige, köp domän, registrera domän, billig domän Sverige, .se domän, köp .se, domänregistrering">
    <meta property="og:title" content="Namnverket — Hitta ett namn som faktiskt är ledigt">
    <meta property="og:description" content="Kolla bolagsnamn, domäner och varumärken i ett slag.">
    <meta property="og:url" content="https://namnverket.se">
    <meta property="og:type" content="website">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://namnverket.se">
    <link rel="alternate" hreflang="sv" href="https://namnverket.se/" />
    <meta property="og:image" content="https://namnverket.se/og-bild.svg">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "WebApplication",
      "name": "Namnverket",
      "description": "Kolla om ett företagsnamn är ledigt hos Bolagsverket, domäner och varumärken i ett slag.",
      "url": "https://namnverket.se",
      "applicationCategory": "BusinessApplication",
      "offers": {
        "@type": "Offer",
        "price": "0",
        "priceCurrency": "SEK"
      }
    }
    </script>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Product",
      "name": "Domänregistrering",
      "description": "Registrera .se och .com domäner direkt via Namnverket",
      "offers": {
        "@type": "AggregateOffer",
        "lowPrice": "149",
        "highPrice": "499",
        "priceCurrency": "SEK",
        "offerCount": "4"
      }
    }
    </script>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "FAQPage",
      "mainEntity": [
        {"@type":"Question","name":"Hur kollar jag om ett företagsnamn är ledigt?","acceptedAnswer":{"@type":"Answer","text":"Skriv in ditt önskade namn i sökfältet på Namnverket. Vi kontrollerar automatiskt Bolagsverkets register, domäntillgänglighet och varumärkesskydd i ett slag."}},
        {"@type":"Question","name":"Vad kostar det att registrera en .se-domän?","acceptedAnswer":{"@type":"Answer","text":"En .se-domän kostar 149 kr per år hos Namnverket. Registreringen sker direkt och domänen är aktiv inom några minuter."}},
        {"@type":"Question","name":"Vad är skillnaden mellan bolagsnamn och domännamn?","acceptedAnswer":{"@type":"Answer","text":"Ett bolagsnamn registreras hos Bolagsverket och skyddar ditt företagsnamn juridiskt i Sverige. Ett domännamn är din adress på internet. Du behöver båda — och det är enklast om de matchar."}},
        {"@type":"Question","name":"Hur lång tid tar det att registrera ett bolagsnamn?","acceptedAnswer":{"@type":"Answer","text":"Namnverket kontrollerar omedelbart om ett bolagsnamn är ledigt. Själva bolagsregistreringen görs sedan hos Bolagsverket eller Verksamt.se och tar normalt 1-3 arbetsdagar."}},
        {"@type":"Question","name":"Vad betyder AI-analysen Mjuka världen?","acceptedAnswer":{"@type":"Answer","text":"Mjuka världen är vår AI-drivna namnanalys som undersöker om ditt företagsnamn betyder något oönskat på andra språk, hur lätt det är att uttala internationellt, och vilka kulturella associationer det väcker."}},
        {"@type":"Question","name":"Kan jag registrera en domän direkt på Namnverket?","acceptedAnswer":{"@type":"Answer","text":"Ja! Om en domän visas som ledig kan du registrera den direkt på Namnverket med kort. Vi hanterar registreringen åt dig via vårt partnerskap med en ackrediterad domänregistrar."}},
        {"@type":"Question","name":"Vad är ett varumärke och behöver jag skydda mitt?","acceptedAnswer":{"@type":"Answer","text":"Ett varumärke ger dig ensamrätt till ditt namn eller logotyp inom en viss bransch. Namnverket kollar automatiskt om ditt namn redan är registrerat som varumärke hos PRV (Sverige) och TMview (EU)."}},
        {"@type":"Question","name":"Vad är tokens och hur fungerar betalningen?","acceptedAnswer":{"@type":"Answer","text":"Grundkollar av bolagsnamn och domäner är alltid gratis. Tokens används för AI-drivna funktioner som namnanalys och namnkonfiguratorn. Du köper tokens i förväg — 50 tokens för 19 kr, 200 för 49 kr eller 500 för 99 kr."}}
      ]
    }
    </script>
    <style>
        :root {
            --svart: #0a0a0a;
            --text-sekunder: #6b6b6b;
            --text-tertiar: #a0a0a0;
            --border: rgba(0,0,0,0.08);
            --gron: #16a34a;
            --rod: #dc2626;
            --amber: #d97706;
            --yta: #f9f9f8;
        }
        *, *::before, *::after { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; max-width: 580px; margin: 80px auto 120px; padding: 0 24px; color: var(--svart); }
        .logo { font-size: 11px; letter-spacing: 0.15em; color: var(--text-tertiar); margin-bottom: 2rem; font-weight: 400; }
        h1 { font-size: 36px; font-weight: 500; letter-spacing: -0.02em; line-height: 1.15; margin-bottom: 12px; }
        .sub { font-size: 15px; color: var(--text-sekunder); margin-bottom: 28px; }
        .input-rad { display: flex; gap: 8px; }
        .input-rad input { flex: 1; height: 48px; padding: 0 14px; font-size: 15px; font-family: 'Inter', sans-serif; border: 1px solid var(--border); border-radius: 8px; outline: none; color: var(--svart); background: #fff; }
        .input-rad input:focus { outline: none; border-color: rgba(0,0,0,0.3); box-shadow: 0 0 0 3px rgba(0,0,0,0.06); }
        .input-rad button { height: 48px; padding: 0 22px; background: var(--svart); color: #fff; border: none; border-radius: 8px; font-size: 14px; font-family: 'Inter', sans-serif; font-weight: 500; cursor: pointer; white-space: nowrap; margin: 0; width: auto; }
        .input-rad button:hover { background: #1a1a1a; }
        .slumpa-btn { height: 48px; padding: 0 18px; background: transparent; color: var(--svart); border: 0.5px solid rgba(0,0,0,0.2); border-radius: 8px; font-size: 14px; font-family: 'Inter', sans-serif; font-weight: 400; cursor: pointer; white-space: nowrap; margin: 0; width: auto; }
        .slumpa-btn:hover { background: #fafafa; border-color: rgba(0,0,0,0.4); }
        .gen-link { display: inline-block; margin-top: 14px; font-size: 13px; color: var(--text-tertiar); text-decoration: none; }
        .gen-link:hover { color: var(--svart); }
        #result { margin-top: 40px; }
        .rad { display: flex; justify-content: space-between; align-items: baseline; padding: 13px 0; border-bottom: 0.5px solid var(--border); font-size: 14px; }
        .rad.top { align-items: flex-start; }
        .rad > span:first-child, .rad > div.lbl { color: var(--text-sekunder); }
        .ok { color: var(--gron); }
        .fel { color: var(--rod); }
        .varning { color: var(--amber); }
        .varning a { color: var(--amber); }
        .laddar { color: var(--text-tertiar); }
        .liknande { color: var(--amber); font-size: 13px; text-align: right; max-width: 65%; }
        .match-lista { text-align: right; max-width: 65%; }
        .match-rad { font-size: 12px; padding: 2px 0; color: var(--text-tertiar); line-height: 1.5; }
        .match-rad a { color: var(--amber); }
        .sub-besk { display: block; font-size: 11px; color: var(--text-tertiar); margin-top: 2px; }
        .reg-btn { border: 0.5px solid rgba(0,0,0,0.15); border-radius: 999px; padding: 4px 14px; font-size: 11px; font-family: 'Inter', sans-serif; background: none; color: var(--svart); cursor: pointer; margin-left: 12px; }
        .reg-btn:hover { background: var(--yta); }
        .reg-box { padding: 10px 0; display: flex; align-items: center; gap: 12px; border-bottom: 0.5px solid var(--border); font-size: 13px; color: var(--text-sekunder); }
        .reg-box .bekrafta { border-radius: 999px; border: none; padding: 5px 16px; font-size: 11px; font-family: 'Inter', sans-serif; background: var(--svart); color: #fff; cursor: pointer; margin: 0; width: auto; }
        .reg-box .bekrafta:hover { background: #1a1a1a; }
        .reg-box .bekrafta:disabled { background: var(--text-tertiar); cursor: not-allowed; }
        #analys-box { margin-top: 28px; padding: 1.25rem 1.5rem; background: var(--yta); border-radius: 12px; }
        .mjuka-label { font-size: 11px; letter-spacing: 0.12em; color: var(--text-tertiar); margin-bottom: 10px; font-weight: 400; }
        .mjuka-text { font-size: 14px; color: var(--text-sekunder); line-height: 1.75; }
        .mjuka-text h3 { font-size: 13px; font-weight: 500; color: var(--text-sekunder); margin: 10px 0 4px; }
        .ext-lank { font-size: 13px; color: var(--text-tertiar); text-decoration: none; }
        .ext-lank:hover { color: var(--svart); }
        .token-bar { display: flex; justify-content: space-between; align-items: center; margin-top: 48px; padding-top: 20px; border-top: 0.5px solid var(--border); font-size: 13px; color: var(--text-tertiar); }
        .token-kop { border: 0.5px solid rgba(0,0,0,0.15); border-radius: 999px; padding: 5px 16px; font-size: 12px; font-family: 'Inter', sans-serif; background: none; color: var(--svart); cursor: pointer; }
        .token-kop:hover { background: var(--yta); }
        .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.3); z-index: 100; align-items: center; justify-content: center; }
        .modal-overlay.open { display: flex; }
        .modal { background: #fff; border-radius: 16px; padding: 32px; width: 100%; max-width: 400px; margin: 0 24px; }
        .modal h2 { font-size: 20px; font-weight: 500; margin-bottom: 6px; }
        .modal .modal-sub { font-size: 14px; color: var(--text-sekunder); margin-bottom: 24px; }
        .paket-rad { display: flex; justify-content: space-between; align-items: center; padding: 14px 0; border-bottom: 0.5px solid var(--border); }
        .paket-rad:last-of-type { border-bottom: none; }
        .paket-info .paket-namn { font-size: 15px; font-weight: 500; }
        .paket-info .paket-tokens { font-size: 13px; color: var(--text-sekunder); margin-top: 2px; }
        .paket-kop { border: none; background: var(--svart); color: #fff; border-radius: 999px; padding: 7px 18px; font-size: 13px; font-family: 'Inter', sans-serif; font-weight: 500; cursor: pointer; white-space: nowrap; }
        .paket-kop:hover { background: #1a1a1a; }
        .modal-stang { float: right; background: none; border: none; font-size: 20px; cursor: pointer; color: var(--text-tertiar); line-height: 1; padding: 0; margin-top: -4px; }
        .market-rad { font-size: 13px; color: var(--text-sekunder); padding: 8px 0 10px; border-bottom: 0.5px solid var(--border); }
        .market-link { color: var(--svart); font-weight: 500; text-decoration: none; border-bottom: 0.5px solid rgba(0,0,0,0.2); padding-bottom: 1px; }
        .market-link:hover { border-color: var(--svart); }
        #analys-box { margin-top: 20px; padding: 0; background: none; border-radius: 0; align-items: center; gap: 10px; }
        #analys-box.expanded { display: block !important; padding: 1.25rem 1.5rem; background: var(--yta); border-radius: 12px; }
        .mjuka-knapp { border: 0.5px solid rgba(0,0,0,0.15); border-radius: 999px; padding: 8px 18px; font-size: 13px; font-family: 'Inter', sans-serif; background: none; color: var(--svart); cursor: pointer; white-space: nowrap; }
        .mjuka-knapp:hover { background: var(--yta); }
        .token-kostnad { font-size: 11px; color: var(--text-tertiar); white-space: nowrap; }
        .faq { margin-top: 52px; padding-top: 36px; border-top: 0.5px solid var(--border); }
        .faq-rubrik { font-size: 11px; letter-spacing: 0.12em; color: var(--text-tertiar); margin-bottom: 20px; font-weight: 400; }
        details { border-bottom: 0.5px solid var(--border); transition: background 0.15s ease; border-radius: 6px; padding: 0 8px; margin: 0 -8px; }
        details:hover { background: #fafafa; }
        details summary { font-size: 14px; padding: 14px 0; cursor: pointer; list-style: none; display: flex; justify-content: space-between; align-items: center; user-select: none; }
        details summary::-webkit-details-marker { display: none; }
        details summary::after { content: '+'; color: var(--text-tertiar); font-size: 18px; font-weight: 300; margin-left: 12px; flex-shrink: 0; }
        details[open] summary::after { content: '−'; }
        details p { font-size: 14px; color: var(--text-sekunder); line-height: 1.75; padding-bottom: 16px; margin: 0; }
        @media (max-width: 600px) {
            body { padding: 0 16px; margin-top: 40px; }
            h1 { font-size: 28px; }
            input { font-size: 16px; }
            .input-rad { flex-direction: column; }
            .rad { flex-direction: column; gap: 4px; }
            .rad span:last-child { text-align: left; }
            button { width: 100%; }
        }
    </style>
</head>
<body>
    <header style="display:flex;justify-content:space-between;align-items:center;">
        <p class="logo" style="margin:0;">NAMNVERKET</p>
        <span style="font-size:12px;color:var(--text-tertiar);cursor:pointer;" onclick="oppnaModal()">
            <strong id="token-antal">—</strong>&nbsp;tokens
        </span>
    </header>
    <main>
    <h1>Hitta ett namn som faktiskt är ledigt.</h1>
    <p class="sub">Kolla bolagsnamn, domäner och varumärken i ett slag.</p>
    <div class="input-rad">
        <input type="text" id="namn" placeholder="t.ex. fiskbularna" />
        <button onclick="kolla()">kolla</button>
        <button class="slumpa-btn" onclick="slumpa()">slumpa →</button>
    </div>
    <a href="/generator" class="gen-link">&#x2756; Namnkonfigurator</a>
    <a href="/favoriter" class="gen-link" style="margin-left:16px;">&#x2665; sparade</a>
    <a href="/trender" class="gen-link" style="margin-left:16px;">&#x2191; trender</a>
    <a href="/domanmarknaden" class="gen-link" style="margin-left:16px;">&#x25C8; domänmarknaden</a>
    <a href="/marknadsplats" class="gen-link" style="margin-left:16px;">&#x21C4; marknadsplats</a>
    <a href="/salj" class="gen-link" style="margin-left:16px;">&#x2B; sälj din domän</a>
    <div id="result"></div>
    <div id="analys-box" style="display:none;"></div>

    <section class="faq" aria-label="Vanliga frågor">
        <p class="faq-rubrik">VANLIGA FRÅGOR</p>
        <details>
            <summary>Hur kollar jag om ett företagsnamn är ledigt?</summary>
            <p>Skriv in ditt önskade namn i sökfältet ovan. Vi kontrollerar automatiskt Bolagsverkets register med 3 miljoner bolag, domäntillgänglighet (.se, .com, .io, .ai) och varumärkesskydd hos PRV och EU — allt i ett enda sökning.</p>
        </details>
        <details>
            <summary>Vad kostar det att registrera en .se-domän?</summary>
            <p>En .se-domän kostar 149 kr per år hos Namnverket. Registreringen sker direkt och domänen är aktiv inom några minuter. <a href="/registrera-doman-se" style="color:inherit;border-bottom:0.5px solid rgba(0,0,0,0.2);">Läs mer om .se-domäner →</a></p>
        </details>
        <details>
            <summary>Vad är skillnaden mellan bolagsnamn och domännamn?</summary>
            <p>Ett bolagsnamn registreras hos Bolagsverket och ger dig juridiskt skydd för ditt företagsnamn i Sverige. Ett domännamn är din adress på internet, t.ex. mittföretag.se. Du behöver båda — och det är enklast om de matchar.</p>
        </details>
        <details>
            <summary>Hur lång tid tar det att registrera ett bolagsnamn?</summary>
            <p>Namnverket kontrollerar omedelbart om ett bolagsnamn är ledigt. Själva bolagsregistreringen görs sedan hos Bolagsverket eller Verksamt.se och tar normalt 1–3 arbetsdagar. <a href="/kolla-foretagsnamn" style="color:inherit;border-bottom:0.5px solid rgba(0,0,0,0.2);">Läs om hur namnkollet fungerar →</a></p>
        </details>
        <details>
            <summary>Kan jag registrera en domän direkt på Namnverket?</summary>
            <p>Ja! Om en domän visas som ledig kan du registrera den direkt med kort. Vi hanterar registreringen åt dig via ett partnerskap med en ackrediterad domänregistrar. Domänen är aktiv inom några minuter.</p>
        </details>
        <details>
            <summary>Vad är ett varumärke och behöver jag skydda mitt?</summary>
            <p>Ett varumärke ger dig ensamrätt till ditt namn eller logotyp inom en viss bransch. Namnverket kollar automatiskt om ditt namn redan är registrerat hos PRV (Sverige) och TMview (EU). <a href="/vad-ar-ett-varumarke" style="color:inherit;border-bottom:0.5px solid rgba(0,0,0,0.2);">Allt om varumärkesskydd →</a></p>
        </details>
        <details>
            <summary>Vad är tokens och hur fungerar betalningen?</summary>
            <p>Grundkollar av bolagsnamn och domäner är alltid gratis. Tokens används för AI-drivna funktioner som namnanalys och namnkonfiguratorn. Du köper tokens i förväg — 50 tokens för 19 kr, 200 för 49 kr eller 500 för 99 kr.</p>
        </details>
        <details>
            <summary>Vad betyder AI-analysen "Mjuka världen"?</summary>
            <p>Mjuka världen är vår AI-drivna namnanalys som undersöker om ditt företagsnamn betyder något oönskat på andra språk, hur lätt det är att uttala internationellt, och vilka kulturella associationer det väcker — viktigt om du planerar att verka utanför Sverige.</p>
        </details>
    </section>
    </main>

    <footer>
    <div class="token-bar">
        <span id="token-visning" style="color:var(--text-tertiar);font-size:13px;">Behöver du fler tokens?</span>
        <button class="token-kop" onclick="oppnaModal()">Köp tokens</button>
    </div>
    </footer>

    <div class="modal-overlay" id="modal-overlay" onclick="stangModal(event)">
        <div class="modal">
            <button class="modal-stang" onclick="stangModalDirekt()">&#x2715;</button>
            <h2>Köp tokens</h2>
            <p class="modal-sub">Tokens används för AI-funktioner. Grundkoll är alltid gratis.</p>
            <div class="paket-rad">
                <div class="paket-info">
                    <div class="paket-namn">Bas &mdash; 19 kr</div>
                    <div class="paket-tokens">50 tokens &nbsp;·&nbsp; 25 analyser eller 16 genereringar</div>
                </div>
                <button class="paket-kop" onclick="location.href='/kop/bas'">Köp</button>
            </div>
            <div class="paket-rad">
                <div class="paket-info">
                    <div class="paket-namn">Standard &mdash; 49 kr</div>
                    <div class="paket-tokens">200 tokens &nbsp;·&nbsp; 100 analyser eller 66 genereringar</div>
                </div>
                <button class="paket-kop" onclick="location.href='/kop/standard'">Köp</button>
            </div>
            <div class="paket-rad">
                <div class="paket-info">
                    <div class="paket-namn">Pro &mdash; 99 kr</div>
                    <div class="paket-tokens">500 tokens &nbsp;·&nbsp; 250 analyser eller 166 genereringar</div>
                </div>
                <button class="paket-kop" onclick="location.href='/kop/pro'">Köp</button>
            </div>
        </div>
    </div>

    <script>
        document.getElementById('namn').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') kolla();
        });

        function renderMarkdown(text) {
            text = text.replace(/^## (.+)$/gm, '<h3>$1</h3>');
            text = text.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
            text = text.replace(/\\*(.+?)\\*/g, '<em>$1</em>');
            text = text.replace(/\\n/g, '<br>');
            return text;
        }

        function kortBeskriv(text, max) {
            if (!text || text.length <= max) return text;
            var cut = text.slice(0, max);
            var lastSpace = cut.lastIndexOf(' ');
            return (lastSpace > max * 0.7 ? cut.slice(0, lastSpace) : cut) + '…';
        }

        function upd(id, html) {
            var el = document.getElementById(id);
            if (el) el.innerHTML = html;
        }

        async function slumpa() {
            console.log('[slumpa] anropar /slumpa?json=1');
            try {
                var r = await fetch('/slumpa?json=1');
                var d = await r.json();
                console.log('[slumpa] svar:', d);
                if (d.namn) {
                    document.getElementById('namn').value = d.namn;
                    kolla();
                }
            } catch(e) { console.error('[slumpa] fel:', e); }
        }

        async function kolla() {
            var namn = document.getElementById('namn').value.trim();
            console.log('[kolla] namn=' + namn);
            if (!namn) return;
            document.getElementById('result').innerHTML = '<p class="laddar" style="font-size:14px;padding-top:16px;">kollar...</p>';
            document.getElementById('analys-box').style.display = 'none';

            var res = await fetch('/kolla?namn=' + encodeURIComponent(namn));
            var data = await res.json();

            var slug = namn.toLowerCase().replace(/\\s+/g, '');

            var html = '';
            html += radBolag(data.bolag, data.bolag_matchande);
            if (data.bolag_liknande && data.bolag_liknande.length > 0) {
                html += '<div class="rad"><span>liknande namn</span><span class="liknande">' + data.bolag_liknande.join(', ') + '</span></div>';
            }
            html += rad('.se-domän', data.se, slug + '.se', data.se_market);
            html += rad('.com-domän', data.com, slug + '.com', data.com_market);
            html += '<div id="wb-se" class="rad"><span>wayback .se</span><span class="laddar">kollar...</span></div>';
            html += '<div id="wb-com" class="rad"><span>wayback .com</span><span class="laddar">kollar...</span></div>';
            html += '<div id="safebrowsing" class="rad"><span>sucuri sitecheck</span><span class="laddar">kollar...</span></div>';
            html += '<div id="wiki" class="rad top"><span>wikipedia</span><span class="laddar">kollar...</span></div>';
            html += '<div id="wipo" class="rad top"><span>wipo globalt varumärke</span><span class="laddar">kollar...</span></div>';
            html += '<div id="tmview" class="rad top"><span>eu-varumärken (tmview)</span><span class="laddar">kollar...</span></div>';
            html += '<div id="prv" class="rad top"><span>svenska varumärken (prv)</span><span class="laddar">kollar...</span></div>';

            var smPlats = [
                ['instagram', 'https://instagram.com/' + slug],
                ['tiktok',    'https://tiktok.com/@' + slug],
                ['x',         'https://x.com/' + slug],
                ['linkedin',  'https://linkedin.com/company/' + slug],
            ];
            smPlats.forEach(function(p) {
                html += '<div class="rad"><span>' + p[0] + '</span>' +
                        '<a href="' + p[1] + '" target="_blank" class="ext-lank">kolla →</a></div>';
            });

            document.getElementById('result').innerHTML = html;

            var box = document.getElementById('analys-box');
            box.innerHTML =
                '<button class="mjuka-knapp" onclick="triggerAnalys()">' +
                '&#x2756; Analysera med Mjuka världen</button>' +
                '<span class="token-kostnad">2 tokens</span>';
            box.style.display = 'flex';

            fetchWayback(slug);
            fetchSafeBrowsing(slug);
            fetchWikipedia(namn);
            fetchWipo(namn);
            fetchTmview(namn);
            fetchPrv(namn);
        }

        async function triggerAnalys() {
            var namn = document.getElementById('namn').value.trim();
            console.log('[triggerAnalys] namn=' + namn);
            if (!namn) return;
            var box = document.getElementById('analys-box');
            box.classList.add('expanded');
            box.innerHTML = '<p class="mjuka-label">MJUKA VÄRLDEN</p><p class="mjuka-text laddar">kollar...</p>';
            box.style.display = 'block';
            await fetchAnalys(namn);
        }

        async function fetchWayback(slug) {
            try {
                var r = await fetch('/wayback?slug=' + encodeURIComponent(slug));
                var d = await r.json();
                ['se', 'com'].forEach(function(tld) {
                    var info = d[tld];
                    var html;
                    if (info && info.historik) {
                        html = '<span>wayback .' + tld + '</span><span class="varning">historik &mdash; <a href="' + info.url + '" target="_blank">visa snapshot</a></span>';
                    } else {
                        html = '<span>wayback .' + tld + '</span><span class="ok">ingen historik</span>';
                    }
                    upd('wb-' + tld, html);
                });
            } catch(e) {
                ['se', 'com'].forEach(function(tld) {
                    upd('wb-' + tld, '<span>wayback .' + tld + '</span><span class="ok">ingen historik</span>');
                });
            }
        }

        async function fetchSafeBrowsing(slug) {
            try {
                var r = await fetch('/safebrowsing?slug=' + encodeURIComponent(slug));
                var d = await r.json();
                var html;
                if (d.flaggad === null || d.flaggad === undefined) {
                    html = '<span>sucuri sitecheck</span><span class="laddar">okänd</span>';
                } else if (d.flaggad) {
                    var detalj = 'betyg ' + (d.betyg || '?');
                    if (d.hot) detalj += ', ' + d.hot;
                    if (d.blacklists && d.blacklists.length > 0) {
                        detalj += ', ' + d.blacklists.join(', ');
                    }
                    html = '<span>sucuri sitecheck</span><span class="fel">flaggad &mdash; ' + detalj + '</span>';
                } else {
                    html = '<span>sucuri sitecheck</span><span class="ok">inte flaggad &middot; betyg ' + (d.betyg || '?') + '</span>';
                }
                upd('safebrowsing', html);
            } catch(e) {
                upd('safebrowsing', '<span>sucuri sitecheck</span><span class="laddar">okänd</span>');
            }
        }

        async function fetchWikipedia(namn) {
            try {
                var r = await fetch('/wikipedia?namn=' + encodeURIComponent(namn));
                var d = await r.json();
                var hittade = [];
                ['sv', 'en'].forEach(function(lang) {
                    var info = d[lang];
                    if (info && info.finns) {
                        var samm = kortBeskriv(info.sammanfattning, 120);
                        hittade.push('<div class="match-rad"><span class="varning">' + lang + '</span> &mdash; <a href="' + info.url + '" target="_blank">' + samm + '</a></div>');
                    }
                });
                if (hittade.length > 0) {
                    upd('wiki', '<span>wikipedia</span><div class="match-lista">' + hittade.join('') + '</div>');
                    document.getElementById('wiki').className = 'rad top';
                } else {
                    upd('wiki', '<span>wikipedia</span><span class="ok">inget känt begrepp</span>');
                    document.getElementById('wiki').className = 'rad';
                }
            } catch(e) {
                upd('wiki', '<span>wikipedia</span><span class="laddar">okänd</span>');
            }
        }

        async function fetchWipo(namn) {
            try {
                var r = await fetch('/wipo?namn=' + encodeURIComponent(namn));
                var d = await r.json();
                var vl = d.varumarken;
                if (vl === null || vl === undefined) {
                    upd('wipo', '<span>wipo globalt varumärke</span><span class="laddar">okänd</span>');
                } else if (vl.length === 0) {
                    upd('wipo', '<span>wipo globalt varumärke</span><span class="ok">inget internationellt varumärke</span>');
                    document.getElementById('wipo').className = 'rad';
                } else {
                    var rader = vl.map(function(v) {
                        return '<div class="match-rad"><span class="varning">' + v.namn + '</span>' + (v.land ? ' <span>' + v.land + '</span>' : '') + '</div>';
                    });
                    upd('wipo', '<span>wipo globalt varumärke</span><div class="match-lista">' + rader.join('') + '</div>');
                    document.getElementById('wipo').className = 'rad top';
                }
            } catch(e) {
                upd('wipo', '<span>wipo globalt varumärke</span><span class="laddar">okänd</span>');
            }
        }

        async function fetchTmview(namn) {
            try {
                var r = await fetch('/tmview?namn=' + encodeURIComponent(namn));
                var d = await r.json();
                var vl = d.varumarken;
                if (vl === null || vl === undefined) {
                    upd('tmview', '<span>eu-varumärken (tmview)</span><span class="laddar">okänd</span>');
                } else if (vl.length === 0) {
                    upd('tmview', '<span>eu-varumärken (tmview)</span><span class="ok">inget eu-varumärke hittat</span>');
                    document.getElementById('tmview').className = 'rad';
                } else {
                    var rader = vl.map(function(v) {
                        return '<div class="match-rad"><span class="varning">' + v.namn + '</span>' + (v.land ? ' <span>' + v.land + '</span>' : '') + '</div>';
                    });
                    upd('tmview', '<span>eu-varumärken (tmview)</span><div class="match-lista">' + rader.join('') + '</div>');
                }
            } catch(e) {
                upd('tmview', '<span>eu-varumärken (tmview)</span><span class="laddar">okänd</span>');
            }
        }

        async function fetchPrv(namn) {
            try {
                var r = await fetch('/prv?namn=' + encodeURIComponent(namn));
                var d = await r.json();
                var vl = d.varumarken;
                if (vl === null || vl === undefined) {
                    upd('prv', '<span>svenska varumärken (prv)</span><span class="laddar">okänd</span>');
                } else if (vl.length === 0) {
                    upd('prv', '<span>svenska varumärken (prv)</span><span class="ok">inget svenskt varumärke hittat</span>');
                    document.getElementById('prv').className = 'rad';
                } else {
                    var rader = vl.map(function(v) {
                        var extra = v.status ? ' &middot; ' + v.status + (v.klass ? ', klass ' + v.klass : '') : '';
                        return '<div class="match-rad"><span class="varning">' + v.namn + '</span>' + extra + '</div>';
                    });
                    upd('prv', '<span>svenska varumärken (prv)</span><div class="match-lista">' + rader.join('') + '</div>');
                    document.getElementById('prv').className = 'rad top';
                }
            } catch(e) {
                upd('prv', '<span>svenska varumärken (prv)</span><span class="laddar">okänd</span>');
            }
        }

        async function fetchAnalys(namn) {
            var box = document.getElementById('analys-box');
            box.classList.add('expanded');
            try {
                var ar = await fetch('/analys?namn=' + encodeURIComponent(namn));
                var ad = await ar.json();
                if (ar.status === 402 || (ad.error && ad.error.toLowerCase().includes('token'))) {
                    box.classList.remove('expanded');
                    box.innerHTML =
                        '<button class="mjuka-knapp" onclick="triggerAnalys()">' +
                        '&#x2756; Analysera med Mjuka världen</button>' +
                        '<span class="token-kostnad">2 tokens</span>';
                    box.style.display = 'flex';
                    oppnaModal();
                    return;
                }
                box.innerHTML = '<p class="mjuka-label">MJUKA VÄRLDEN</p><div class="mjuka-text">' + renderMarkdown(ad.analys || '') + '</div>';
                hämtaTokens();
            } catch(e) {
                box.innerHTML = '<p class="mjuka-label">MJUKA VÄRLDEN</p><p class="mjuka-text">kunde inte hämta analys.</p>';
            }
        }

        async function visaPris(doman, boxId) {
            var box = document.getElementById(boxId);
            box.style.display = 'flex';
            box.innerHTML = '<span class="laddar">hämtar pris...</span>';
            try {
                var r = await fetch('/op_pris?doman=' + encodeURIComponent(doman));
                var d = await r.json();
                if (d.error) {
                    box.innerHTML = '<span class="fel">' + d.error + '</span>';
                    return;
                }
                box.innerHTML = '<span>' + d.kundpris + ' kr/år</span>' +
                    '<a class="bekrafta" style="display:inline-block;text-decoration:none;" href="/kop-doman?doman=' + encodeURIComponent(doman) + '">Köp ' + doman + ' →</a>';
            } catch(e) {
                box.innerHTML = '<span class="laddar">okänd</span>';
            }
        }

        async function registrera(doman, btn) {
            btn.disabled = true;
            btn.textContent = 'registrerar...';
            try {
                var r = await fetch('/registrera', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({doman: doman})
                });
                var d = await r.json();
                var box = btn.parentElement;
                if (d.ok) {
                    box.innerHTML = '<span class="ok">domänen registrerades.</span>';
                } else {
                    box.innerHTML = '<span class="fel">' + (d.error || 'okänd') + '</span>';
                }
            } catch(e) {
                btn.disabled = false;
                btn.textContent = 'bekräfta';
            }
        }

        function radBolag(ledig, matchande) {
            if (ledig === null) {
                return '<div class="rad"><span>bolagsnamn (bolagsverket)</span><span class="laddar">kollar...</span></div>';
            }
            if (ledig) {
                return '<div class="rad"><span>bolagsnamn (bolagsverket)</span><span class="ok">ledigt</span></div>';
            }
            var rader = (matchande || []).slice(0, 3).map(function(m) {
                var besk = m.beskrivning ? '<span class="sub-besk">' + kortBeskriv(m.beskrivning, 150) + '</span>' : '';
                return '<div class="match-rad"><span class="fel">upptaget</span> &mdash; ' + m.namn + besk + '</div>';
            });
            if (rader.length === 0) {
                rader.push('<div class="match-rad"><span class="fel">upptaget</span></div>');
            }
            return '<div class="rad top"><span>bolagsnamn (bolagsverket)</span><div class="match-lista">' + rader.join('') + '</div></div>';
        }

        function rad(label, ledig, doman, market) {
            if (ledig === null) {
                return '<div class="rad"><span>' + label + '</span><span class="laddar">okänd</span></div>';
            }
            if (!ledig) {
                var extra = '';
                if (market && market.pris) {
                    extra = '<div class="market-rad">&#x1F4B0; Till salu för <strong>' + market.pris.toLocaleString('sv-SE') + ' kr</strong> &mdash; <a href="/kop-begagnad/' + encodeURIComponent(doman) + '" class="market-link">Köp direkt →</a></div>';
                }
                return '<div class="rad"><span>' + label + '</span><span class="fel">upptaget</span></div>' + extra;
            }
            var boxId = 'reg-' + doman.replace('.', '-');
            return '<div class="rad"><span>' + label + '</span>' +
                '<span><span class="ok">ledigt</span>' +
                '<button class="reg-btn" onclick="visaPris(\\'' + doman + '\\', \\'' + boxId + '\\')">registrera</button>' +
                '</span></div>' +
                '<div id="' + boxId + '" class="reg-box" style="display:none;"></div>';
        }

        async function hämtaTokens() {
            try {
                var r = await fetch('/tokens');
                var d = await r.json();
                document.getElementById('token-antal').textContent = d.tokens;
            } catch(e) {}
        }

        function oppnaModal() {
            console.log('[oppnaModal] öppnar modal');
            var el = document.getElementById('modal-overlay');
            if (!el) { console.error('[oppnaModal] #modal-overlay hittades inte!'); return; }
            el.classList.add('open');
        }

        function stangModalDirekt() {
            document.getElementById('modal-overlay').classList.remove('open');
        }

        function stangModal(e) {
            if (e.target === document.getElementById('modal-overlay')) stangModalDirekt();
        }

        hämtaTokens();

        (function() {
            var params = new URLSearchParams(window.location.search);
            var namnParam = params.get('namn');
            if (namnParam) {
                document.getElementById('namn').value = namnParam;
                kolla();
            }
        })();
    </script>
</body>
</html>
'''

def kolla_bolag(namn):
    con = sqlite3.connect(DB)
    cur = con.cursor()

    matchande = cur.execute(
        "SELECT namn, beskrivning FROM bolag WHERE namn LIKE ? COLLATE NOCASE LIMIT 5",
        (f'%{namn}%',)
    ).fetchall()

    prefix_len = min(4, len(namn))
    prefix = namn[:prefix_len]
    candidates = cur.execute(
        "SELECT namn FROM bolag WHERE namn LIKE ? COLLATE NOCASE LIMIT 5000",
        (f'{prefix}%',)
    ).fetchall()
    con.close()

    namn_lower = namn.lower()
    exact_lower = {m[0].lower() for m in matchande}
    liknande = []
    for (candidate,) in candidates:
        cand_lower = candidate.lower()
        if namn_lower in cand_lower or cand_lower in exact_lower:
            continue
        if SequenceMatcher(None, namn_lower, cand_lower).ratio() >= 0.8:
            liknande.append(candidate)

    return {
        'ledig': len(matchande) == 0,
        'matchande': [{'namn': m[0], 'beskrivning': m[1]} for m in matchande],
        'liknande': liknande[:5],
    }

def kolla_doman(doman):
    try:
        tld = doman.split('.')[-1]
        if tld == 'se':
            namn = doman[:-(len(tld) + 1)]
            r = requests.get(f'http://free.iis.se/free?q={namn}.se', timeout=5)
            return 'free' in r.text.lower()
        else:
            r = requests.get(f'https://rdap.verisign.com/com/v1/domain/{doman}', timeout=5)
            return r.status_code == 404
    except Exception:
        return None

def _wayback_kolla(domain):
    try:
        r = requests.get(
            f'https://archive.org/wayback/available?url={domain}',
            timeout=8
        )
        if r.status_code != 200:
            return {'historik': False, 'url': None}
        snap = r.json().get('archived_snapshots', {}).get('closest', {})
        if snap.get('available'):
            return {'historik': True, 'url': snap['url']}
        return {'historik': False, 'url': None}
    except Exception:
        return {'historik': False, 'url': None}

def _wikipedia_kolla(lang, namn):
    url = f'https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(namn)}'
    try:
        r = requests.get(url, timeout=5, headers={'User-Agent': 'Namnkoll/1.0'})
        if r.status_code == 200:
            data = r.json()
            return {
                'finns': True,
                'sammanfattning': data.get('extract', ''),
                'url': data.get('content_urls', {}).get('desktop', {}).get('page', ''),
            }
        return {'finns': False}
    except Exception:
        return {'finns': None}

@app.route('/')
def index():
    sid = get_session_id()
    resp = make_response(render_template_string(HTML))
    resp.set_cookie('sid', sid, max_age=60 * 60 * 24 * 365, samesite='Lax')
    return resp

@app.route('/tokens')
def tokens_route():
    sid = get_session_id()
    return jsonify({'tokens': get_tokens(sid)})

@app.route('/kolla')
@limiter.limit('30 per minute')
def kolla():
    namn = _sanera_text(request.args.get('namn', '').strip())
    if not namn:
        return jsonify({'error': 'Inget namn'})

    slug = namn.lower().replace(' ', '')
    bolag = kolla_bolag(namn)
    se = kolla_doman(f'{slug}.se')
    com = kolla_doman(f'{slug}.com')

    ledig = bolag['ledig'] or (isinstance(se, dict) and se.get('ledig')) or (isinstance(com, dict) and com.get('ledig'))
    try:
        con = sqlite3.connect(DB)
        con.execute('INSERT INTO sokningar (namn, hittade_ledigt) VALUES (?, ?)', (namn, bool(ledig)))
        con.commit()
        con.close()
    except Exception:
        pass

    def _market_kolla(doman):
        try:
            con = sqlite3.connect(DB)
            row = con.execute(
                "SELECT pris FROM domanmarknaden WHERE doman=? AND status='aktiv'",
                (doman,)
            ).fetchone()
            con.close()
            return {'pris': row[0]} if row else None
        except Exception:
            return None

    se_market  = _market_kolla(f'{slug}.se')  if se  is False else None
    com_market = _market_kolla(f'{slug}.com') if com is False else None

    return jsonify({
        'bolag': bolag['ledig'],
        'bolag_matchande': bolag['matchande'],
        'bolag_liknande': bolag['liknande'],
        'se': se,
        'com': com,
        'se_market': se_market,
        'com_market': com_market,
    })

@app.route('/wayback')
def wayback():
    slug = request.args.get('slug', '').strip()
    if not slug:
        return jsonify({'error': 'Ingen slug'})

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {tld: ex.submit(_wayback_kolla, f'{slug}.{tld}') for tld in ['se', 'com']}
        result = {tld: futures[tld].result() for tld in ['se', 'com']}

    return jsonify(result)

@app.route('/wikipedia')
def wikipedia():
    namn = request.args.get('namn', '').strip()
    if not namn:
        return jsonify({'error': 'Inget namn'})

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {lang: ex.submit(_wikipedia_kolla, lang, namn) for lang in ['sv', 'en']}
        result = {lang: futures[lang].result() for lang in ['sv', 'en']}

    return jsonify(result)

@app.route('/tmview')
def tmview():
    namn = request.args.get('namn', '').strip()
    if not namn:
        return jsonify({'error': 'Inget namn'})

    try:
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Origin': 'https://www.tmdn.org',
            'Referer': 'https://www.tmdn.org/tmview/',
        })
        r = s.post(
            'https://www.tmdn.org/tmview/api/search/results',
            json={'basicSearch': namn, 'pageSize': 5, 'pageNumber': 1},
            timeout=8
        )
        if r.status_code != 200:
            return jsonify({'varumarken': None})

        data = r.json()
        tms = (data.get('tradeMarks') or {}).get('list') or data.get('results') or []
        if not tms:
            return jsonify({'varumarken': []})

        result = []
        for tm in tms[:5]:
            name = (tm.get('tradeMarkName') or tm.get('wordMark') or '').strip()
            offices = tm.get('officeCodes') or []
            land = offices[0] if offices else tm.get('tradeMarkCountryCode', '')
            if name:
                result.append({'namn': name, 'land': land})

        return jsonify({'varumarken': result})
    except Exception:
        return jsonify({'varumarken': None})

@app.route('/wipo')
def wipo():
    namn = request.args.get('namn', '').strip()
    if not namn:
        return jsonify({'error': 'Inget namn'})

    try:
        r = requests.get(
            'https://branddb.wipo.int/branddb/en/quicksearch.json',
            params={'query': namn, 'rows': 5},
            headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json',
            },
            timeout=8
        )
        ct = r.headers.get('Content-Type', '')
        if r.status_code != 200 or 'json' not in ct:
            return jsonify({'varumarken': None})

        data = r.json()
        docs = (data.get('response') or {}).get('docs') or data.get('results') or []
        if not docs:
            return jsonify({'varumarken': []})

        result = []
        for doc in docs[:5]:
            name = (doc.get('markName') or doc.get('brandName') or
                    doc.get('name') or doc.get('mark') or '').strip()
            country = (doc.get('countryCode') or doc.get('country') or
                       doc.get('officeCode') or '').strip()
            if name:
                result.append({'namn': name, 'land': country})

        return jsonify({'varumarken': result})
    except Exception:
        return jsonify({'varumarken': None})

@app.route('/prv')
def prv():
    namn = request.args.get('namn', '').strip()
    if not namn:
        return jsonify({'error': 'Inget namn'})

    try:
        con = sqlite3.connect(DB)
        cur = con.cursor()
        rows = cur.execute(
            "SELECT namn, status, klass FROM varumärken WHERE namn LIKE ? COLLATE NOCASE LIMIT 5",
            (f'%{namn}%',)
        ).fetchall()
        con.close()

        if not rows:
            return jsonify({'varumarken': []})

        return jsonify({
            'varumarken': [{'namn': r[0], 'status': r[1], 'klass': r[2]} for r in rows]
        })
    except Exception:
        return jsonify({'varumarken': None})

def _sucuri_kolla(domain):
    try:
        r = requests.get(
            f'https://sitecheck.sucuri.net/api/v3/?scan={domain}&json=1',
            timeout=10,
            headers={'User-Agent': 'Namnkoll/1.0'}
        )
        if r.status_code != 200:
            return None
        data = r.json()
        ratings = data.get('ratings') or {}
        rating = (ratings.get('total') or {}).get('rating') or \
                 (ratings.get('security') or {}).get('rating')
        if not rating:
            return None

        flaggad = rating not in ('A', 'B')

        blacklists = [
            bl.get('vendor', '')
            for bl in (data.get('blacklists') or [])
            if bl.get('vendor')
        ]

        hot_typ = None
        warnings = (data.get('warnings') or {}).get('security') or {}
        malware_list = warnings.get('malware') or []
        if malware_list:
            hot_typ = malware_list[0].get('type') or malware_list[0].get('msg')

        return {
            'flaggad': flaggad,
            'betyg': rating,
            'hot': hot_typ,
            'blacklists': blacklists,
        }
    except Exception:
        return None

@app.route('/safebrowsing')
def safebrowsing():
    slug = request.args.get('slug', '').strip()
    if not slug:
        return jsonify({'error': 'Ingen slug'})

    result = _sucuri_kolla(f'{slug}.se')
    if result is None:
        result = _sucuri_kolla(f'{slug}.com')

    if result is None:
        return jsonify({'flaggad': None})
    return jsonify(result)

@app.route('/op_pris')
def op_pris():
    doman = request.args.get('doman', '').strip()
    print(f'[OP_PRIS] doman="{doman}"', flush=True)
    if not doman or '.' not in doman:
        return jsonify({'error': 'Ogiltig domän'})
    try:
        grossistpris, kundpris, valuta = hämta_pris(doman)
        print(f'[OP_PRIS] grossist={grossistpris} kundpris={kundpris} {valuta}', flush=True)
        return jsonify({'grossistpris': grossistpris, 'kundpris': kundpris, 'valuta': valuta})
    except Exception as e:
        print(f'[OP_PRIS] Fel: {e}', flush=True)
        return jsonify({'error': str(e)})

@app.route('/registrera', methods=['POST'])
def registrera():
    body = request.get_json() or {}
    doman = body.get('doman', '').strip()
    print(f'[REGISTRERA] Mottaget doman="{doman}"', flush=True)

    if not doman or '.' not in doman:
        return jsonify({'ok': False, 'error': 'Ogiltig domän'})

    namn, ext = doman.split('.', 1)
    print(f'[REGISTRERA] Split → namn="{namn}" ext="{ext}"', flush=True)

    try:
        token = get_op_token()
        handle = get_op_handle()
        payload = {
            'domain': {'name': namn, 'extension': ext},
            'period': 1,
            'owner_handle': handle,
            'name_servers': [
                {'name': 'ns1.openprovider.eu'},
                {'name': 'ns2.openprovider.eu'},
                {'name': 'ns3.openprovider.eu'},
            ],
            'additional_data': {
                'iisse_acceptance': '1',
            },
        }
        import json as _json
        print(f'[REGISTRERA] Exakt JSON som skickas:\n{_json.dumps(payload, indent=2, ensure_ascii=False)}', flush=True)
        r = requests.post(
            'https://api.openprovider.eu/v1beta/domains',
            headers={'Authorization': f'Bearer {token}'},
            json=payload,
            timeout=15
        )
        print(f'[REGISTRERA] Svar HTTP {r.status_code}: {r.text[:300]}', flush=True)
        data = r.json()
        if data.get('code') == 0:
            return jsonify({'ok': True})
        return jsonify({'ok': False, 'error': data.get('desc', 'API-fel')})
    except Exception as e:
        print(f'[REGISTRERA] Exception: {e}', flush=True)
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/analys')
def analys():
    namn = _sanera_text(request.args.get('namn', '').strip())
    if not namn:
        return jsonify({'error': 'Inget namn'})

    sid = get_session_id()
    if not deduct_tokens(sid, 2):
        return jsonify({'error': 'Du behöver fler tokens'}), 402

    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    message = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=400,
        messages=[{
            'role': 'user',
            'content': (
                f"Analysera företagsnamnet '{namn}' kort på svenska. "
                "Täck: 1) Betyder det något på andra språk (särskilt tyska, franska, arabiska, spanska)? "
                "2) Finns negativa konnotationer? "
                "3) Är det lätt att uttala internationellt? "
                "4) Passar det för ett företag? "
                "5) Sociala medier: ge en riskpoäng 1-10 för hur sannolikt det är att namnet redan är taget "
                "på Instagram, TikTok, X och LinkedIn. 1 = troligen ledigt överallt, 10 = nästan säkert taget. "
                "Motivera kort. Format: \"Sociala medier: X/10 — [motivering]\" "
                "Var kortfattad, max 5-6 meningar totalt."
            )
        }]
    )

    return jsonify({'analys': message.content[0].text})

GENERATOR_HTML = '''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Namnkonfigurator &mdash; Namnverket</title>
    <meta name="description" content="Generera skräddarsydda namnförslag för ditt bolag med AI. Välj bransch, känsla och stil — få unika företagsnamn direkt.">
    <meta property="og:title" content="Namnkonfigurator — Namnverket">
    <meta property="og:description" content="Generera skräddarsydda namnförslag för ditt bolag med AI.">
    <meta property="og:url" content="https://namnverket.se/generator">
    <meta property="og:type" content="website">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://namnverket.se/generator">
    <link rel="alternate" hreflang="sv" href="https://namnverket.se/generator" />
    <meta property="og:image" content="https://namnverket.se/og-bild.svg">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --svart: #0a0a0a;
            --text-sekunder: #6b6b6b;
            --text-tertiar: #a0a0a0;
            --border: rgba(0,0,0,0.08);
            --yta: #f9f9f8;
            --gron: #16a34a;
            --rod: #dc2626;
        }
        *, *::before, *::after { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; max-width: 580px; margin: 80px auto 120px; padding: 0 24px; color: var(--svart); }
        .logo { font-size: 11px; letter-spacing: 0.15em; color: var(--text-tertiar); margin-bottom: 2rem; font-weight: 400; }
        .back { font-size: 13px; color: var(--text-tertiar); text-decoration: none; display: inline-block; margin-bottom: 28px; }
        .back:hover { color: var(--svart); }
        h1 { font-size: 36px; font-weight: 500; letter-spacing: -0.02em; line-height: 1.15; margin-bottom: 10px; }
        .sub { font-size: 15px; color: var(--text-sekunder); margin-bottom: 32px; }
        .slump-sektion { margin-top: 48px; padding-top: 36px; border-top: 0.5px solid var(--border); }
        .slump-sektion h2 { font-size: 20px; font-weight: 500; letter-spacing: -0.01em; margin-bottom: 8px; }
        .slump-sektion .slump-desc { font-size: 14px; color: var(--text-sekunder); margin-bottom: 20px; }
        .slump-btn { display: inline-block; height: 44px; padding: 0 24px; line-height: 44px; background: none; color: var(--svart); border: 0.5px solid rgba(0,0,0,0.2); border-radius: 8px; font-size: 14px; font-family: \'Inter\', sans-serif; font-weight: 500; cursor: pointer; text-decoration: none; }
        .slump-btn:hover { background: var(--yta); }
        @media (max-width: 600px) {
            body { padding: 0 16px; margin-top: 40px; }
            h1 { font-size: 28px; }
            input { font-size: 16px; }
            .rad { flex-direction: column; gap: 4px; }
            .rad span:last-child { text-align: left; }
            button { width: 100%; }
            .slump-btn { display: block; width: 100%; text-align: center; }
        }
        .falt { margin-bottom: 20px; }
        .falt label { display: block; font-size: 12px; color: var(--text-tertiar); margin-bottom: 10px; letter-spacing: 0.04em; }
        .falt label .valfri { font-size: 11px; color: var(--text-tertiar); letter-spacing: 0; font-weight: 400; }
        .falt select, .falt input[type=text] {
            width: 100%; height: 44px; padding: 0 12px;
            font-size: 14px; font-family: 'Inter', sans-serif;
            border: 0.5px solid rgba(0,0,0,0.15); border-radius: 8px;
            background: #fff; color: var(--svart); outline: none;
            appearance: none; -webkit-appearance: none;
        }
        .falt select:focus, .falt input[type=text]:focus { border-color: rgba(0,0,0,0.3); }
        .falt input[type=text]::placeholder { color: var(--text-tertiar); }
        .pill-grupp { display: flex; flex-wrap: wrap; gap: 8px; }
        .pill {
            border: 0.5px solid rgba(0,0,0,0.15); border-radius: 999px;
            padding: 7px 16px; font-size: 13px; font-family: 'Inter', sans-serif;
            background: #fff; color: var(--svart); cursor: pointer;
            user-select: none; transition: background 0.1s, border-color 0.1s, color 0.1s;
        }
        .pill:hover { background: var(--yta); }
        .pill.vald { background: var(--svart); color: #fff; border-color: var(--svart); }
        button.gen {
            margin-top: 28px; width: 100%; height: 48px;
            background: var(--svart); color: #fff; border: none;
            border-radius: 8px; font-size: 14px; font-family: 'Inter', sans-serif;
            font-weight: 500; cursor: pointer;
        }
        button.gen:hover { background: #1a1a1a; }
        button.gen:disabled { background: var(--text-tertiar); cursor: not-allowed; }
        #resultat { margin-top: 36px; }
        .batch-rubrik {
            font-size: 11px; letter-spacing: 0.1em; color: var(--text-tertiar);
            padding: 24px 0 10px; border-top: 0.5px solid var(--border);
        }
        .namn-rad {
            display: flex; justify-content: space-between; align-items: center;
            padding: 11px 0; border-bottom: 0.5px solid var(--border);
        }
        .namn-rad .namn-text { font-size: 16px; font-weight: 500; }
        .namn-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
        .tumme {
            border: 0.5px solid rgba(0,0,0,0.12); border-radius: 999px;
            padding: 3px 9px; font-size: 12px; background: none;
            color: var(--text-tertiar); cursor: pointer; font-family: 'Inter', sans-serif;
            transition: background 0.1s, color 0.1s, border-color 0.1s;
        }
        .tumme:hover { background: var(--yta); }
        .tumme.gilla-aktiv { background: var(--gron); color: #fff; border-color: var(--gron); }
        .tumme.ogilla-aktiv { background: var(--rod); color: #fff; border-color: var(--rod); }
        .namn-kolla { font-size: 13px; color: var(--text-tertiar); text-decoration: none; white-space: nowrap; }
        .namn-kolla:hover { color: var(--svart); }
        .forfina-box {
            margin-top: 32px; padding-top: 28px; border-top: 0.5px solid var(--border);
            display: none;
        }
        .forfina-label { font-size: 12px; color: var(--text-tertiar); letter-spacing: 0.04em; margin-bottom: 10px; }
        textarea {
            width: 100%; min-height: 72px; padding: 10px 12px;
            font-size: 14px; font-family: 'Inter', sans-serif;
            border: 0.5px solid rgba(0,0,0,0.15); border-radius: 8px;
            resize: vertical; outline: none; color: var(--svart); background: #fff;
        }
        textarea::placeholder { color: var(--text-tertiar); }
        textarea:focus { border-color: rgba(0,0,0,0.3); }
        .gen-fler {
            margin-top: 12px; width: 100%; height: 44px;
            background: none; color: var(--svart);
            border: 0.5px solid rgba(0,0,0,0.2); border-radius: 8px;
            font-size: 14px; font-family: 'Inter', sans-serif; font-weight: 500; cursor: pointer;
        }
        .gen-fler:hover { background: var(--yta); }
        .gen-fler:disabled { color: var(--text-tertiar); border-color: var(--border); cursor: not-allowed; }
        .laddar { font-size: 14px; color: var(--text-tertiar); padding-top: 16px; }
        .fel { font-size: 14px; color: var(--rod); padding-top: 16px; }
        .pill-grupp-sektion { margin-bottom: 14px; }
        .pill-grupp-rubrik { font-size: 11px; color: var(--text-tertiar); letter-spacing: 0.06em; margin-bottom: 7px; }
        .falt-hint { font-size: 12px; color: var(--text-tertiar); margin-top: 6px; }
        #bransch-annat-box { margin-top: 10px; }
        .spara-btn {
            border: 0.5px solid rgba(0,0,0,0.12); border-radius: 999px;
            padding: 3px 9px; font-size: 13px; background: none;
            color: var(--text-tertiar); cursor: pointer; font-family: 'Inter', sans-serif;
            transition: color 0.12s, border-color 0.12s;
        }
        .spara-btn:hover { color: #e11d48; border-color: #e11d48; }
        .spara-btn.sparad { color: #e11d48; border-color: #e11d48; }
        .email-modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.3); z-index: 100; align-items: center; justify-content: center; }
        .email-modal-overlay.open { display: flex; }
        .email-modal { background: #fff; border-radius: 16px; padding: 28px; width: 100%; max-width: 360px; margin: 0 24px; }
        .email-modal h3 { font-size: 17px; font-weight: 500; margin-bottom: 6px; }
        .email-modal .em-sub { font-size: 13px; color: var(--text-sekunder); margin-bottom: 16px; }
        .email-modal input[type=email] { width: 100%; height: 44px; padding: 0 12px; font-size: 14px; font-family: 'Inter', sans-serif; border: 0.5px solid rgba(0,0,0,0.15); border-radius: 8px; outline: none; color: var(--svart); background: #fff; }
        .email-modal input[type=email]:focus { border-color: rgba(0,0,0,0.3); }
        .email-modal-actions { display: flex; gap: 8px; margin-top: 12px; }
        .email-modal-actions button { flex: 1; height: 40px; border-radius: 8px; font-size: 13px; font-family: 'Inter', sans-serif; font-weight: 500; cursor: pointer; border: none; }
        .em-spara { background: var(--svart); color: #fff; }
        .em-avbryt { background: var(--yta); color: var(--svart); }
        .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.3); z-index: 200; align-items: center; justify-content: center; }
        .modal-overlay.open { display: flex; }
        .modal { background: #fff; border-radius: 16px; padding: 32px; width: 100%; max-width: 400px; margin: 0 24px; }
        .modal h2 { font-size: 20px; font-weight: 500; margin-bottom: 6px; }
        .modal .modal-sub { font-size: 14px; color: var(--text-sekunder); margin-bottom: 24px; }
        .paket-rad { display: flex; justify-content: space-between; align-items: center; padding: 14px 0; border-bottom: 0.5px solid var(--border); }
        .paket-rad:last-of-type { border-bottom: none; }
        .paket-info .paket-namn { font-size: 15px; font-weight: 500; }
        .paket-info .paket-tokens { font-size: 13px; color: var(--text-sekunder); margin-top: 2px; }
        .paket-kop { border: none; background: var(--svart); color: #fff; border-radius: 999px; padding: 7px 18px; font-size: 13px; font-family: 'Inter', sans-serif; font-weight: 500; cursor: pointer; white-space: nowrap; }
        .paket-kop:hover { background: #1a1a1a; }
        .modal-stang { float: right; background: none; border: none; font-size: 20px; cursor: pointer; color: var(--text-tertiar); line-height: 1; padding: 0; margin-top: -4px; }
    </style>
</head>
<body>
    <header>
        <p class="logo">NAMNVERKET</p>
    </header>
    <main>
    <a class="back" href="/">← tillbaka</a>
    <h1>Namnkonfigurator</h1>
    <p class="sub">Beskriv ditt bolag och få skräddarsydda namnförslag</p>

    <div class="falt">
        <label>BRANSCH</label>
        <div class="pill-grupp-sektion">
            <div class="pill-grupp-rubrik">DIGITAL &amp; TECH</div>
            <div class="pill-grupp">
                <button class="pill" data-grupp="bransch" data-varde="Mjukvaruutveckling" onclick="valPillBransch(this)">Mjukvaruutveckling</button>
                <button class="pill" data-grupp="bransch" data-varde="Digital marknadsföring" onclick="valPillBransch(this)">Digital marknadsföring</button>
                <button class="pill" data-grupp="bransch" data-varde="IT-konsult" onclick="valPillBransch(this)">IT-konsult</button>
                <button class="pill" data-grupp="bransch" data-varde="AI &amp; data" onclick="valPillBransch(this)">AI &amp; data</button>
                <button class="pill" data-grupp="bransch" data-varde="Cybersäkerhet" onclick="valPillBransch(this)">Cybersäkerhet</button>
            </div>
        </div>
        <div class="pill-grupp-sektion">
            <div class="pill-grupp-rubrik">KREATIVT &amp; MEDIA</div>
            <div class="pill-grupp">
                <button class="pill" data-grupp="bransch" data-varde="Design &amp; byrå" onclick="valPillBransch(this)">Design &amp; byrå</button>
                <button class="pill" data-grupp="bransch" data-varde="Foto &amp; film" onclick="valPillBransch(this)">Foto &amp; film</button>
                <button class="pill" data-grupp="bransch" data-varde="Innehåll &amp; kommunikation" onclick="valPillBransch(this)">Innehåll &amp; kommunikation</button>
            </div>
        </div>
        <div class="pill-grupp-sektion">
            <div class="pill-grupp-rubrik">HÄLSA &amp; LIVSSTIL</div>
            <div class="pill-grupp">
                <button class="pill" data-grupp="bransch" data-varde="Hälsa &amp; träning" onclick="valPillBransch(this)">Hälsa &amp; träning</button>
                <button class="pill" data-grupp="bransch" data-varde="Mat &amp; dryck" onclick="valPillBransch(this)">Mat &amp; dryck</button>
                <button class="pill" data-grupp="bransch" data-varde="Skönhet &amp; mode" onclick="valPillBransch(this)">Skönhet &amp; mode</button>
            </div>
        </div>
        <div class="pill-grupp-sektion">
            <div class="pill-grupp-rubrik">PROFESSIONELLA TJÄNSTER</div>
            <div class="pill-grupp">
                <button class="pill" data-grupp="bransch" data-varde="Juridik &amp; redovisning" onclick="valPillBransch(this)">Juridik &amp; redovisning</button>
                <button class="pill" data-grupp="bransch" data-varde="Rekrytering &amp; HR" onclick="valPillBransch(this)">Rekrytering &amp; HR</button>
                <button class="pill" data-grupp="bransch" data-varde="Utbildning &amp; coaching" onclick="valPillBransch(this)">Utbildning &amp; coaching</button>
            </div>
        </div>
        <div class="pill-grupp-sektion">
            <div class="pill-grupp-rubrik">HANDEL &amp; INDUSTRI</div>
            <div class="pill-grupp">
                <button class="pill" data-grupp="bransch" data-varde="E-handel" onclick="valPillBransch(this)">E-handel</button>
                <button class="pill" data-grupp="bransch" data-varde="Bygg &amp; fastighet" onclick="valPillBransch(this)">Bygg &amp; fastighet</button>
                <button class="pill" data-grupp="bransch" data-varde="Tillverkning" onclick="valPillBransch(this)">Tillverkning</button>
            </div>
        </div>
        <div class="pill-grupp-sektion">
            <div class="pill-grupp">
                <button class="pill" data-grupp="bransch" data-varde="Annat" onclick="valPillBransch(this)">Annat</button>
            </div>
        </div>
        <div id="bransch-annat-box" style="display:none;">
            <input type="text" id="bransch-annat" placeholder="Beskriv din bransch..." />
        </div>
    </div>

    <div class="falt">
        <label>TYP</label>
        <div class="pill-grupp" id="typ">
            <button class="pill vald" data-varde="Produkt" onclick="valPill(this,'typ')">Produkt</button>
            <button class="pill" data-varde="Tjänst" onclick="valPill(this,'typ')">Tjänst</button>
            <button class="pill" data-varde="Konsult" onclick="valPill(this,'typ')">Konsult</button>
            <button class="pill" data-varde="Både och" onclick="valPill(this,'typ')">Både och</button>
        </div>
    </div>

    <div class="falt">
        <label>MÅLGRUPP</label>
        <div class="pill-grupp" id="malgrupp">
            <button class="pill vald" data-varde="Företag (B2B)" onclick="valPill(this,'malgrupp')">Företag (B2B)</button>
            <button class="pill" data-varde="Konsumenter (B2C)" onclick="valPill(this,'malgrupp')">Konsumenter (B2C)</button>
            <button class="pill" data-varde="Båda" onclick="valPill(this,'malgrupp')">Båda</button>
        </div>
    </div>

    <div class="falt">
        <label>KÄNSLA <span class="valfri">(välj upp till 2)</span></label>
        <div class="pill-grupp" id="kansla">
            <button class="pill" data-varde="Trygg &amp; etablerad" onclick="valMultiPill(this,'kansla',2)">Trygg &amp; etablerad</button>
            <button class="pill" data-varde="Lekfull &amp; modern" onclick="valMultiPill(this,'kansla',2)">Lekfull &amp; modern</button>
            <button class="pill" data-varde="Premium &amp; exklusiv" onclick="valMultiPill(this,'kansla',2)">Premium &amp; exklusiv</button>
            <button class="pill" data-varde="Enkel &amp; tillgänglig" onclick="valMultiPill(this,'kansla',2)">Enkel &amp; tillgänglig</button>
            <button class="pill" data-varde="Teknisk &amp; innovativ" onclick="valMultiPill(this,'kansla',2)">Teknisk &amp; innovativ</button>
        </div>
    </div>

    <div class="falt">
        <label>RÄCKVIDD</label>
        <div class="pill-grupp" id="rackvidd">
            <button class="pill vald" data-varde="Bara Sverige" onclick="valPill(this,'rackvidd')">Bara Sverige</button>
            <button class="pill" data-varde="Internationellt" onclick="valPill(this,'rackvidd')">Internationellt</button>
            <button class="pill" data-varde="Båda" onclick="valPill(this,'rackvidd')">Båda</button>
        </div>
    </div>

    <div class="falt">
        <label>NYCKELORD <span class="valfri">(valfritt)</span></label>
        <input type="text" id="nyckelord" placeholder="hav, snabb, grön..." />
    </div>

    <div class="falt">
        <label>INSPIRATION <span class="valfri">(valfritt)</span></label>
        <input type="text" id="inspiration" placeholder="T.ex. Klarna, Oatly, Spotify..." />
    </div>

    <div class="falt">
        <label>BETYDELSE <span class="valfri">(valfritt)</span></label>
        <input type="text" id="betydelse" placeholder="T.ex. frihet, rörelse, ljus, styrka, enkelhet..." />
        <p class="falt-hint">Vad vill du att namnet ska associera till eller betyda?</p>
    </div>

    <div class="falt">
        <label>UNDVIK <span class="valfri">(valfritt)</span></label>
        <input type="text" id="undvik" placeholder="T.ex. Tech, Smart, Pro..." />
    </div>

    <div class="falt">
        <label>LÄNGD</label>
        <select id="langd">
            <option value="kort">Kort &mdash; max 6 bokstäver</option>
            <option value="medium" selected>Medium &mdash; 7&ndash;10 bokstäver</option>
            <option value="spelar ingen roll">Spelar ingen roll</option>
        </select>
    </div>

    <button class="gen" onclick="generera(false)">Generera förslag</button>
    <span style="font-size:12px;color:#a0a0a0;margin-left:10px;">3 tokens</span>
    <div id="resultat"></div>

    <div class="email-modal-overlay" id="email-modal-overlay">
        <div class="email-modal">
            <h3>Spara namn</h3>
            <p class="em-sub">Ange din e-post för att spara favoriter och se dem på alla enheter.</p>
            <input type="email" id="email-modal-input" placeholder="din@email.se" />
            <div class="email-modal-actions">
                <button class="em-avbryt" onclick="emailModalAvbryt()">Avbryt</button>
                <button class="em-spara" onclick="emailModalSpara()">Spara</button>
            </div>
        </div>
    </div>

    <div class="forfina-box" id="forfina-box">
        <p class="forfina-label">VAD VILL DU HA MER ELLER MINDRE AV?</p>
        <textarea id="feedback" placeholder="T.ex. mer lekfullt, kortare, mindre tech-känsla..."></textarea>
        <button class="gen-fler" id="gen-fler-btn" onclick="generera(true)">Generera fler förslag</button>
    </div>

    <div class="slump-sektion">
        <h2>Slumpgenerator</h2>
        <p class="slump-desc">Känner du dig lycklig? Låt oss välja åt dig.</p>
        <a class="slump-btn" href="/slumpa">Slumpa ett namn →</a>
    </div>

    <script>
        var state = { gillade: {}, ogillat: {}, allaNamn: [], runda: 0, favoriter: '' };

        function valPill(el, grupp) {
            document.querySelectorAll('#' + grupp + ' .pill').forEach(function(p) { p.classList.remove('vald'); });
            el.classList.add('vald');
        }

        function valPillBransch(el) {
            document.querySelectorAll('.pill[data-grupp="bransch"]').forEach(function(p) { p.classList.remove('vald'); });
            el.classList.add('vald');
            document.getElementById('bransch-annat-box').style.display =
                el.dataset.varde === 'Annat' ? 'block' : 'none';
        }

        function getBransch() {
            var vald = document.querySelector('.pill[data-grupp="bransch"].vald');
            if (!vald) return '';
            if (vald.dataset.varde === 'Annat') {
                return document.getElementById('bransch-annat').value.trim() || 'Annat';
            }
            return vald.dataset.varde;
        }

        function valMultiPill(el, grupp, max) {
            if (el.classList.contains('vald')) {
                el.classList.remove('vald');
            } else {
                var valda = document.querySelectorAll('#' + grupp + ' .pill.vald');
                if (valda.length < max) el.classList.add('vald');
            }
        }

        function getPillVarde(grupp) {
            var el = document.querySelector('#' + grupp + ' .pill.vald');
            return el ? el.dataset.varde : '';
        }

        function getMultiPillVarden(grupp) {
            return Array.from(document.querySelectorAll('#' + grupp + ' .pill.vald'))
                .map(function(p) { return p.dataset.varde; }).join(', ');
        }

        function gilla(namn, gillaBtn, ogillaBtn) {
            if (gillaBtn.classList.contains('gilla-aktiv')) {
                gillaBtn.classList.remove('gilla-aktiv');
                delete state.gillade[namn];
            } else {
                gillaBtn.classList.add('gilla-aktiv');
                state.gillade[namn] = true;
                ogillaBtn.classList.remove('ogilla-aktiv');
                delete state.ogillat[namn];
            }
        }

        function ogilla(namn, ogillaBtn, gillaBtn) {
            if (ogillaBtn.classList.contains('ogilla-aktiv')) {
                ogillaBtn.classList.remove('ogilla-aktiv');
                delete state.ogillat[namn];
            } else {
                ogillaBtn.classList.add('ogilla-aktiv');
                state.ogillat[namn] = true;
                gillaBtn.classList.remove('gilla-aktiv');
                delete state.gillade[namn];
            }
        }

        function skapaNamnRad(namn) {
            var rad = document.createElement('div');
            rad.className = 'namn-rad';
            var namnEl = document.createElement('span');
            namnEl.className = 'namn-text';
            namnEl.textContent = namn;
            var actions = document.createElement('div');
            actions.className = 'namn-actions';
            var gillaBtn = document.createElement('button');
            gillaBtn.className = 'tumme';
            gillaBtn.textContent = '+';
            gillaBtn.title = 'Gilla';
            var ogillaBtn = document.createElement('button');
            ogillaBtn.className = 'tumme';
            ogillaBtn.textContent = '−';
            ogillaBtn.title = 'Ogilla';
            gillaBtn.onclick = function() { gilla(namn, gillaBtn, ogillaBtn); };
            ogillaBtn.onclick = function() { ogilla(namn, ogillaBtn, gillaBtn); };
            var sparaBtn = document.createElement('button');
            sparaBtn.className = 'spara-btn';
            sparaBtn.textContent = '♡';
            sparaBtn.title = 'Spara';
            (function(n, b) { b.onclick = function() { spara(n, b); }; })(namn, sparaBtn);
            var kollaLink = document.createElement('a');
            kollaLink.className = 'namn-kolla';
            kollaLink.href = '/?namn=' + encodeURIComponent(namn);
            kollaLink.target = '_blank';
            kollaLink.textContent = 'Kolla →';
            actions.appendChild(gillaBtn);
            actions.appendChild(ogillaBtn);
            actions.appendChild(sparaBtn);
            actions.appendChild(kollaLink);
            rad.appendChild(namnEl);
            rad.appendChild(actions);
            return rad;
        }

        function getCookie(name) {
            var v = document.cookie.match('(^|;) ?' + name + '=([^;]*)(;|$)');
            return v ? v[2] : '';
        }

        function setCookie(name, val, days) {
            var d = new Date();
            d.setTime(d.getTime() + days * 24 * 60 * 60 * 1000);
            document.cookie = name + '=' + val + ';expires=' + d.toUTCString() + ';path=/;SameSite=Lax';
        }

        var _sparaPending = null;

        async function spara(namn, btn) {
            var email = getCookie('nk_email');
            if (email) {
                await sparaMedEmail(namn, email, btn);
            } else {
                _sparaPending = { namn: namn, btn: btn };
                document.getElementById('email-modal-overlay').classList.add('open');
                document.getElementById('email-modal-input').value = '';
                setTimeout(function() { document.getElementById('email-modal-input').focus(); }, 80);
            }
        }

        async function sparaMedEmail(namn, email, btn) {
            var prev = btn.textContent;
            btn.textContent = '…';
            try {
                var r = await fetch('/spara', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ namn: namn, email: email, bransch: getBransch(), typ: getPillVarde('typ') })
                });
                var d = await r.json();
                if (d.ok) { btn.textContent = '♥'; btn.classList.add('sparad'); }
                else { btn.textContent = prev; }
            } catch(e) { btn.textContent = prev; }
        }

        function emailModalSpara() {
            var email = document.getElementById('email-modal-input').value.trim();
            if (!email || !email.includes('@')) return;
            setCookie('nk_email', email, 365);
            document.getElementById('email-modal-overlay').classList.remove('open');
            if (_sparaPending) {
                sparaMedEmail(_sparaPending.namn, email, _sparaPending.btn);
                _sparaPending = null;
            }
        }

        function emailModalAvbryt() {
            document.getElementById('email-modal-overlay').classList.remove('open');
            _sparaPending = null;
        }

        document.getElementById('email-modal-input') && document.getElementById('email-modal-input').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') emailModalSpara();
        });

        (function() {
            var p = new URLSearchParams(window.location.search);
            var fav = p.get('favoriter');
            if (fav) {
                state.favoriter = fav;
                var antal = fav.split(',').filter(Boolean).length;
                var banner = document.createElement('p');
                banner.style.cssText = 'font-size:13px;color:var(--text-sekundär);margin:0 0 12px;';
                banner.textContent = antal + ' favoriter laddade — genererar förslag baserade på dem…';
                var resultatEl = document.getElementById('resultat');
                resultatEl.parentNode.insertBefore(banner, resultatEl);
                setTimeout(function() { generera(false); }, 100);
            }
        })();

        async function generera(forfinig) {
            var btn = forfinig ? document.getElementById('gen-fler-btn') : document.querySelector('button.gen');
            btn.disabled = true;
            btn.textContent = 'genererar...';

            if (!forfinig) {
                state.gillade = {};
                state.ogillat = {};
                state.allaNamn = [];
                state.runda = 0;
                document.getElementById('resultat').innerHTML = '';
                document.getElementById('forfina-box').style.display = 'none';
            }

            var laddEl = document.createElement('p');
            laddEl.className = 'laddar';
            laddEl.textContent = 'arbetar...';
            document.getElementById('resultat').appendChild(laddEl);

            var params = new URLSearchParams({
                bransch:    getBransch(),
                typ:        getPillVarde('typ'),
                malgrupp:   getPillVarde('malgrupp'),
                kansla:     getMultiPillVarden('kansla'),
                rackvidd:   getPillVarde('rackvidd'),
                nyckelord:  document.getElementById('nyckelord').value.trim(),
                inspiration:document.getElementById('inspiration').value.trim(),
                betydelse:  document.getElementById('betydelse').value.trim(),
                undvik:     document.getElementById('undvik').value.trim(),
                langd:      document.getElementById('langd').value,
            });

            if (forfinig) {
                params.set('forfinig', '1');
                params.set('gillade', Object.keys(state.gillade).join(','));
                params.set('ogillat', Object.keys(state.ogillat).join(','));
                params.set('feedback', document.getElementById('feedback').value.trim());
                params.set('tidigare_forslag', state.allaNamn.join(','));
            }
            if (state.favoriter && !forfinig) {
                params.set('favoriter', state.favoriter);
            }

            try {
                var r = await fetch('/generera?' + params.toString());
                var d = await r.json();
                laddEl.remove();
                btn.disabled = false;
                btn.textContent = forfinig ? 'Generera fler förslag' : 'Generera förslag';

                if (d.error) {
                    if (r.status === 402 || d.error.toLowerCase().includes('token')) {
                        oppnaModal();
                    } else {
                        var felEl = document.createElement('p');
                        felEl.className = 'fel';
                        felEl.textContent = d.error;
                        document.getElementById('resultat').appendChild(felEl);
                    }
                    return;
                }

                state.runda++;
                var namn = d.namn || [];
                namn.forEach(function(n) {
                    if (state.allaNamn.indexOf(n) === -1) state.allaNamn.push(n);
                });

                var batch = document.createElement('div');
                if (state.runda > 1) {
                    var rubrik = document.createElement('p');
                    rubrik.className = 'batch-rubrik';
                    rubrik.textContent = 'RUNDA ' + state.runda;
                    batch.appendChild(rubrik);
                }
                namn.forEach(function(n) { batch.appendChild(skapaNamnRad(n)); });
                document.getElementById('resultat').appendChild(batch);
                document.getElementById('forfina-box').style.display = 'block';

            } catch(e) {
                laddEl.remove();
                btn.disabled = false;
                btn.textContent = forfinig ? 'Generera fler förslag' : 'Generera förslag';
                var felEl = document.createElement('p');
                felEl.className = 'fel';
                felEl.textContent = 'något gick fel, försök igen.';
                document.getElementById('resultat').appendChild(felEl);
            }
        }

        function oppnaModal() {
            document.getElementById('modal-overlay').classList.add('open');
        }
        function stangModalDirekt() {
            document.getElementById('modal-overlay').classList.remove('open');
        }
        function stangModal(e) {
            if (e.target === document.getElementById('modal-overlay')) stangModalDirekt();
        }
    </script>

    <div class="modal-overlay" id="modal-overlay" onclick="stangModal(event)">
        <div class="modal">
            <button class="modal-stang" onclick="stangModalDirekt()">&#x2715;</button>
            <h2>Köp tokens</h2>
            <p class="modal-sub">Tokens används för namnförslag och namnanalys.</p>
            <div class="paket-rad">
                <div class="paket-info">
                    <div class="paket-namn">Bas</div>
                    <div class="paket-tokens">50 tokens</div>
                </div>
                <button class="paket-kop" onclick="location.href='/kop/bas'">19 kr</button>
            </div>
            <div class="paket-rad">
                <div class="paket-info">
                    <div class="paket-namn">Standard</div>
                    <div class="paket-tokens">200 tokens</div>
                </div>
                <button class="paket-kop" onclick="location.href='/kop/standard'">49 kr</button>
            </div>
            <div class="paket-rad">
                <div class="paket-info">
                    <div class="paket-namn">Pro</div>
                    <div class="paket-tokens">500 tokens</div>
                </div>
                <button class="paket-kop" onclick="location.href='/kop/pro'">99 kr</button>
            </div>
        </div>
    </div>
    </main>
</body>
</html>
'''

@app.route('/generator')
def generator():
    return render_template_string(GENERATOR_HTML)

_SV_STAVELSER = [
    'ab','al','an','ar','be','bi','bo','by','da','de','di','do',
    'ek','el','en','er','fa','fe','fi','fo','ga','ge','gi','go',
    'ha','he','hi','ho','hu','in','ja','je','jo','ka','ke','ki',
    'ko','ku','la','le','li','lo','lu','ma','me','mi','mo','mu',
    'na','ne','ni','no','nu','ok','ol','om','on','or','pa','pe',
    'pi','po','ra','re','ri','ro','ru','sa','se','si','so','su',
    'ta','te','ti','to','tu','un','va','ve','vi','vo','vy',
    'ber','dal','den','dra','fal','for','fra','gen','ger','hal',
    'han','hel','hem','hol','kal','kar','kel','kol','kor','kra',
    'lar','len','ler','lin','lon','mar','mel','men','mil','mor',
    'nel','ner','nil','nor','pal','par','pel','per','pol','ral',
    'ran','rel','ren','rik','rin','rol','ron','sal','sel','sen',
    'ser','sil','sin','sol','son','tal','tan','tel','ten','til',
    'tin','tol','ton','tor','val','van','vel','ven','vil','vin',
    'vol','von',
]

@app.route('/slumpa')
def slumpa():
    import random as _r
    for _ in range(50):
        antal = _r.choices([2, 3], weights=[40, 60])[0]
        namn = ''.join(_r.choice(_SV_STAVELSER) for _ in range(antal)).capitalize()
        if 5 <= len(namn) <= 10:
            if request.args.get('json'):
                return jsonify({'namn': namn})
            return redirect(f'/?namn={quote(namn)}')
    if request.args.get('json'):
        return jsonify({'namn': 'Namnio'})
    return redirect('/')

FAVORITER_HTML = '''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Favoriter &mdash; Namnverket</title>
    <meta name="description" content="Dina sparade namnförslag på Namnverket.">
    <meta name="robots" content="noindex, nofollow">
    <link rel="alternate" hreflang="sv" href="https://namnverket.se/favoriter" />
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
    <style>
        :root {
            --svart: #0a0a0a;
            --text-sekunder: #6b6b6b;
            --text-tertiar: #a0a0a0;
            --border: rgba(0,0,0,0.08);
            --yta: #f9f9f8;
            --rod: #dc2626;
        }
        *, *::before, *::after { box-sizing: border-box; }
        body { font-family: \'Inter\', sans-serif; max-width: 580px; margin: 80px auto 120px; padding: 0 24px; color: var(--svart); }
        .logo { font-size: 11px; letter-spacing: 0.15em; color: var(--text-tertiar); margin-bottom: 2rem; font-weight: 400; }
        .back { font-size: 13px; color: var(--text-tertiar); text-decoration: none; display: inline-block; margin-bottom: 28px; }
        .back:hover { color: var(--svart); }
        h1 { font-size: 36px; font-weight: 500; letter-spacing: -0.02em; line-height: 1.15; margin-bottom: 32px; }
        .tom-text { font-size: 14px; color: var(--text-tertiar); padding: 8px 0 24px; line-height: 1.7; }
        .tom-text a { color: var(--svart); }
        .namn-rad { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 0.5px solid var(--border); }
        .namn-info .namn-text { font-size: 16px; font-weight: 500; }
        .namn-info .namn-meta { font-size: 12px; color: var(--text-tertiar); margin-top: 3px; }
        .namn-actions { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
        .kolla-lank { font-size: 13px; color: var(--text-tertiar); text-decoration: none; white-space: nowrap; }
        .kolla-lank:hover { color: var(--svart); }
        .ta-bort-btn { border: none; background: none; font-size: 19px; color: var(--text-tertiar); cursor: pointer; padding: 2px 4px; line-height: 1; }
        .ta-bort-btn:hover { color: var(--rod); }
        .sektion-rubrik { font-size: 11px; letter-spacing: 0.12em; color: var(--text-tertiar); margin: 52px 0 20px; font-weight: 400; }
        .radar-wrapper { width: 100%; max-width: 380px; margin: 0 auto; }
        .laddar { font-size: 14px; color: var(--text-tertiar); }
        .gen-inline-btn { margin-top: 4px; height: 44px; padding: 0 22px; background: var(--svart); color: #fff; border: none; border-radius: 8px; font-size: 14px; font-family: \'Inter\', sans-serif; font-weight: 500; cursor: pointer; }
        .gen-inline-btn:hover { background: #1a1a1a; }
        .gen-inline-btn:disabled { background: var(--text-tertiar); cursor: not-allowed; }
        .forslag-lista { margin-top: 20px; }
        .forslag-rad { display: flex; justify-content: space-between; align-items: center; padding: 11px 0; border-bottom: 0.5px solid var(--border); font-size: 15px; font-weight: 500; }
        .forslag-actions { display: flex; align-items: center; gap: 14px; flex-shrink: 0; }
        .hjart-btn { border: none; background: none; font-size: 18px; color: var(--text-tertiar); cursor: pointer; padding: 2px 4px; line-height: 1; transition: color 0.15s; }
        .hjart-btn:hover { color: #e11d48; }
        .hjart-btn.sparat { color: #e11d48; }
        .kolla-lnk { font-size: 13px; color: var(--text-tertiar); text-decoration: none; white-space: nowrap; }
        .kolla-lnk:hover { color: var(--svart); }
        @media (max-width: 600px) {
            body { padding: 0 16px; margin-top: 40px; }
            h1 { font-size: 28px; }
            input { font-size: 16px; }
            .rad { flex-direction: column; gap: 4px; }
            .rad span:last-child { text-align: left; }
            button { width: 100%; }
        }
    </style>
</head>
<body>
    <header>
        <p class="logo">NAMNVERKET</p>
    </header>
    <main>
    <a class="back" href="/">← tillbaka</a>
    <h1>Sparade namn</h1>

    <div id="namn-lista"><p class="laddar">laddar...</p></div>

    <p class="sektion-rubrik">NAMNPROFIL</p>
    <div id="radar-box"><p class="laddar">analyserar...</p></div>

    <p class="sektion-rubrik">FÖRSLAG BASERADE PÅ DINA FAVORITER</p>
    <div>
        <button class="gen-inline-btn" id="gen-inline-btn" onclick="genereraInline()">Generera förslag</button>
        <span style="font-size:12px;color:var(--text-tertiar);margin-left:10px;">3 tokens</span>
    </div>
    <div id="forslag-lista" class="forslag-lista"></div>

    <script>
        var sparade = [];

        async function laddaSparade() {
            try {
                var r = await fetch('/sparade');
                var d = await r.json();
                sparade = d.namn || [];
                renderaSparade();
                if (sparade.length >= 3) {
                    laddaRadar();
                } else {
                    document.getElementById(\'radar-box\').innerHTML = \'<p style="font-size:13px;color:var(--text-tertiar);">Spara minst 3 namn för att se namnprofilen.</p>\';
                }
            } catch(e) {
                document.getElementById(\'namn-lista\').innerHTML = \'<p class="laddar">kunde inte ladda sparade namn.</p>\';
                document.getElementById(\'radar-box\').innerHTML = \'\';
            }
        }

        function renderaSparade() {
            var lista = document.getElementById(\'namn-lista\');
            if (sparade.length === 0) {
                lista.innerHTML = \'<p class="tom-text">Du har inga sparade namn än. Gå till <a href="/generator">Namnkonfiguratorn</a> och tryck ♡ på namn du gillar.</p>\';
                return;
            }
            lista.innerHTML = sparade.map(function(n, i) {
                var meta = [n.bransch, n.typ].filter(Boolean).join(\' · \');
                var datum = n.skapad ? n.skapad.split(\' \')[0] : \'\';
                var metaStr = [meta, datum].filter(Boolean).join(\' · \');
                return \'<div class="namn-rad">\' +
                    \'<div class="namn-info">\' +
                        \'<div class="namn-text">\' + n.namn + \'</div>\' +
                        (metaStr ? \'<div class="namn-meta">\' + metaStr + \'</div>\' : \'\') +
                    \'</div>\' +
                    \'<div class="namn-actions">\' +
                        \'<a class="kolla-lank" href="/?namn=\' + encodeURIComponent(n.namn) + \'" target="_blank">Kolla →</a>\' +
                        \'<button class="ta-bort-btn" data-idx="\' + i + \'" onclick="taBort(sparade[this.dataset.idx].namn, this)" title="Ta bort">×</button>\' +
                    \'</div>\' +
                \'</div>\';
            }).join(\'\');
        }

        async function taBort(namn, btn) {
            btn.disabled = true;
            try {
                await fetch(\'/sparade/\' + encodeURIComponent(namn), { method: \'DELETE\' });
                sparade = sparade.filter(function(n) { return n.namn !== namn; });
                renderaSparade();
                if (sparade.length >= 3) {
                    laddaRadar();
                } else {
                    document.getElementById(\'radar-box\').innerHTML = \'<p style="font-size:13px;color:var(--text-tertiar);">Spara minst 3 namn för att se namnprofilen.</p>\';
                    if (window._radarChart) { window._radarChart.destroy(); window._radarChart = null; }
                }
            } catch(e) { btn.disabled = false; }
        }

        async function genereraInline() {
            if (sparade.length === 0) return;
            var btn = document.getElementById(\'gen-inline-btn\');
            var lista = document.getElementById(\'forslag-lista\');
            btn.disabled = true;
            btn.textContent = \'genererar...\';
            lista.innerHTML = \'<p class="laddar" style="padding:12px 0;">Claude analyserar dina favoriter...</p>\';
            try {
                var r = await fetch(\'/generera-favoriter\');
                var d = await r.json();
                if (r.status === 402 || (d.error && d.error.toLowerCase().includes(\'token\'))) {
                    lista.innerHTML = \'<p style="font-size:13px;color:var(--text-tertiar);">Du behöver tokens. <a href="/kop/bas" style="color:#0a0a0a;">Köp 50 tokens för 19 kr →</a></p>\';
                } else if (d.error) {
                    lista.innerHTML = \'<p style="font-size:13px;color:#dc2626;">\' + d.error + \'</p>\';
                } else {
                    lista.innerHTML = (d.namn || []).map(function(n) {
                        return \'<div class="forslag-rad">\' +
                            \'<span>\' + n + \'</span>\' +
                            \'<div class="forslag-actions">\' +
                                \'<button class="hjart-btn" data-namn="\' + n.replace(/"/g, \'&quot;\') + \'" onclick="sparaNamnInline(this)" title="Spara">&#x2661;</button>\' +
                                \'<a class="kolla-lnk" href="/?namn=\' + encodeURIComponent(n) + \'" target="_blank">Kolla →</a>\' +
                            \'</div>\' +
                        \'</div>\';
                    }).join(\'\');
                }
            } catch(e) {
                lista.innerHTML = \'<p style="font-size:13px;color:#dc2626;">Nätverksfel, försök igen.</p>\';
            }
            btn.disabled = false;
            btn.textContent = \'Generera förslag\';
        }

        async function sparaNamnInline(btn) {
            var namn = btn.dataset.namn;
            btn.disabled = true;
            try {
                var r = await fetch(\'/spara-session\', {
                    method: \'POST\',
                    headers: {\'Content-Type\': \'application/json\'},
                    body: JSON.stringify({namn: namn})
                });
                var d = await r.json();
                if (d.ok) {
                    btn.innerHTML = \'&#x2665;\';
                    btn.classList.add(\'sparat\');
                    sparade.push({namn: namn, bransch: \'\', typ: \'\', skapad: \'\'});
                } else {
                    btn.disabled = false;
                }
            } catch(e) { btn.disabled = false; }
        }

        async function laddaRadar() {
            var box = document.getElementById(\'radar-box\');
            box.innerHTML = \'<p class="laddar">analyserar namnprofil... <span style="font-size:11px;color:#a0a0a0;">(2 tokens)</span></p>\';
            try {
                var r = await fetch(\'/analysera_favoriter\');
                var d = await r.json();
                if (r.status === 402 || (d.error && d.error.toLowerCase().includes(\'token\'))) {
                    box.innerHTML = \'<p style="font-size:13px;color:var(--text-tertiar);">Du behöver tokens för namnprofilen. <a href="/kop/bas" style="color:#0a0a0a;">Köp 50 tokens för 19 kr →</a></p>\';
                    return;
                }
                if (d.error) {
                    box.innerHTML = \'<p style="font-size:13px;color:var(--text-tertiar);">\' + d.error + \'</p>\';
                    return;
                }
                renderaRadar(d);
            } catch(e) {
                box.innerHTML = \'<p style="font-size:13px;color:var(--text-tertiar);">kunde inte analysera.</p>\';
            }
        }

        function renderaRadar(p) {
            var box = document.getElementById(\'radar-box\');
            box.innerHTML = \'<div class="radar-wrapper"><canvas id="radar-canvas"></canvas></div>\';
            if (window._radarChart) window._radarChart.destroy();
            var ctx = document.getElementById(\'radar-canvas\').getContext(\'2d\');
            window._radarChart = new Chart(ctx, {
                type: \'radar\',
                data: {
                    labels: [\'Internationellt\', \'Modernt\', \'Kort\', \'Abstrakt\', \'Nordiskt\'],
                    datasets: [{
                        label: \'Namnprofil\',
                        data: [p.internationellt||0, p.modernt||0, p.kort||0, p.abstrakt||0, p.nordiskt||0],
                        backgroundColor: \'rgba(10,10,10,0.07)\',
                        borderColor: \'rgba(10,10,10,0.65)\',
                        borderWidth: 1.5,
                        pointBackgroundColor: \'rgba(10,10,10,0.65)\',
                        pointRadius: 3,
                    }]
                },
                options: {
                    scales: {
                        r: {
                            min: 0, max: 10,
                            ticks: { display: false, stepSize: 2 },
                            grid: { color: \'rgba(0,0,0,0.06)\' },
                            angleLines: { color: \'rgba(0,0,0,0.06)\' },
                            pointLabels: { font: { family: \'Inter\', size: 12 }, color: \'#6b6b6b\' }
                        }
                    },
                    plugins: { legend: { display: false } }
                }
            });
        }

        laddaSparade();
    </script>
    </main>
</body>
</html>
'''

@app.route('/favoriter')
def favoriter():
    return render_template_string(FAVORITER_HTML)


@app.route('/generera-favoriter')
@limiter.limit('10 per minute')
def generera_favoriter():
    sid = get_session_id()
    if not deduct_tokens(sid, 3):
        return jsonify({'error': 'Du behöver fler tokens'}), 402
    con = sqlite3.connect(DB)
    rows = con.execute(
        'SELECT namn FROM sparade_namn WHERE email = ? ORDER BY skapad DESC LIMIT 20',
        (sid,)
    ).fetchall()
    con.close()
    if not rows:
        return jsonify({'error': 'Inga sparade namn att basera på'}), 400
    namn_lista = [r[0] for r in rows]
    try:
        import json as _json, re as _re
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        message = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=200,
            messages=[{'role': 'user', 'content': (
                f'Du är en expert på att skapa företagsnamn. '
                f'Användaren har sparat dessa namn som favoriter: {", ".join(namn_lista)}. '
                f'Analysera vad de har gemensamt i stil, klang och känsla — '
                f'och generera exakt 5 genuint nya företagsnamn i samma anda men olika varandra. '
                f'Svara ENDAST med en JSON-array med exakt 5 strängar, inga förklaringar: '
                f'["Namn1", "Namn2", "Namn3", "Namn4", "Namn5"]'
            )}]
        )
        raw = message.content[0].text.strip()
        match = _re.search(r'\[.*?\]', raw, _re.DOTALL)
        if not match:
            return jsonify({'error': 'Claude svarade i oväntat format'}), 500
        namn = _json.loads(match.group())
        return jsonify({'namn': namn[:5]})
    except Exception as e:
        print(f'[GENERERA-FAVORITER] fel: {e}', flush=True)
        return jsonify({'error': 'Kunde inte generera förslag, försök igen.'}), 500


@app.route('/spara-session', methods=['POST'])
def spara_session():
    sid = get_session_id()
    body = request.get_json() or {}
    namn = _sanera_text(body.get('namn') or '')
    if not namn:
        return jsonify({'ok': False, 'error': 'Namn saknas'})
    try:
        con = sqlite3.connect(DB)
        con.execute(
            'INSERT INTO sparade_namn (email, namn, bransch, typ) VALUES (?, ?, ?, ?)',
            (sid, namn, '', '')
        )
        con.commit()
        con.close()
    except Exception as e:
        print(f'[SPARA-SESSION] fel: {e}', flush=True)
        return jsonify({'ok': False, 'error': 'Kunde inte spara'})
    return jsonify({'ok': True})


@app.route('/generera')
def generera():
    sid = get_session_id()
    forfinig = request.args.get('forfinig', '0') == '1'
    kostnad = 2 if forfinig else 3
    if not deduct_tokens(sid, kostnad):
        return jsonify({'error': 'Du behöver fler tokens'}), 402

    bransch     = _sanera_text(request.args.get('bransch', ''))
    typ         = _sanera_text(request.args.get('typ', ''))
    malgrupp    = _sanera_text(request.args.get('malgrupp', ''))
    kansla      = _sanera_text(request.args.get('kansla', ''))
    rackvidd    = _sanera_text(request.args.get('rackvidd', ''))
    nyckelord   = _sanera_text(request.args.get('nyckelord', ''), max_len=200)
    inspiration = _sanera_text(request.args.get('inspiration', ''), max_len=200)
    betydelse   = _sanera_text(request.args.get('betydelse', ''), max_len=200)
    undvik      = _sanera_text(request.args.get('undvik', ''), max_len=200)
    langd       = _sanera_text(request.args.get('langd', ''))
    gillade     = _sanera_text(request.args.get('gillade', ''), max_len=500)
    ogillat     = _sanera_text(request.args.get('ogillat', ''), max_len=500)
    feedback    = _sanera_text(request.args.get('feedback', ''), max_len=500)
    tidigare    = _sanera_text(request.args.get('tidigare_forslag', ''), max_len=1000)
    favoriter   = _sanera_text(request.args.get('favoriter', ''), max_len=500)

    print(f'[GENERERA] bransch={bransch!r} typ={typ!r} malgrupp={malgrupp!r} kansla={kansla!r} rackvidd={rackvidd!r} nyckelord={nyckelord!r} inspiration={inspiration!r} betydelse={betydelse!r} undvik={undvik!r} langd={langd!r} favoriter={favoriter!r}', flush=True)

    langd_text = {
        'kort':   'Kort — max 6 bokstäver per namn',
        'medium': 'Medium — 7 till 10 bokstäver per namn',
    }.get(langd, 'Längden spelar ingen roll')

    kontext_rader = [
        f'- Bransch: {bransch}' if bransch else '',
        f'- Typ av erbjudande: {typ}' if typ else '',
        f'- Målgrupp: {malgrupp}' if malgrupp else '',
        f'- Känsla/ton: {kansla}' if kansla else '',
        f'- Räckvidd: {rackvidd}' if rackvidd else '',
        f'- Längd: {langd_text}',
        f'- Nyckelord att inspireras av: {nyckelord}' if nyckelord else '',
        f'- Inspireras av dessa varumärken (men kopiera dem inte): {inspiration}' if inspiration else '',
        f'- Önskad betydelse/association: {betydelse}' if betydelse else '',
        f'- Undvik ord eller stilar som: {undvik}' if undvik else '',
    ]
    kontext = '\n'.join(r for r in kontext_rader if r)

    konsult_tillagg = '''
Om konsult: inkludera alternativ med geografisk koppling (stadsnamn, region), kompetensord (Advisory, Partners, Group) och eventuellt grundarnamnsstruktur (Efternamn & Co).''' if typ == 'Konsult' else ''

    betydelse_tillagg = f'\nOm betydelse: "{betydelse}" — låt detta genomsyra associationerna och klangbilden i namnen.' if betydelse else ''

    variation_krav = f'''
VIKTIGT — Variation är avgörande:
- Variera antal stavelser: minst 2 namn med 1–2 stavelser, minst 3 med 3 stavelser
- Variera språklig känsla: minst 2 nordiska/svenska, minst 2 latinska/internationella, minst 2 påhittade/abstrakta
- Variera struktur: sammansatta ord, påhittade ord, verkliga ord med ny kontext
- ALDRIG mer än 2 namn med samma suffix (-ix, -el, -ra, -verk etc)
- Varje namn ska kännas unikt, inte en variation av ett annat namn i listan{konsult_tillagg}{betydelse_tillagg}'''

    favoriter_tillagg = (
        f'\n\nAnvändaren har sparat dessa namn som favoriter: {favoriter}.\n'
        f'Analysera vad de har gemensamt i stil, klang och känsla — och generera 10 namn i samma anda, men genuint nya och olika varandra.'
    ) if favoriter and not forfinig else ''

    if forfinig:
        feedback_rader = []
        if gillade:
            feedback_rader.append(f'Namn som användaren GILLADE: {gillade}')
        if ogillat:
            feedback_rader.append(f'Namn som användaren INTE gillade: {ogillat}')
        if feedback:
            feedback_rader.append(f'Användarens feedback: "{feedback}"')
        if tidigare:
            feedback_rader.append(f'Tidigare genererade namn (generera INTE dessa igen): {tidigare}')

        prompt = f'''Du är en expert på att skapa företagsnamn. Här är kontexten:
{kontext}

Feedback från användaren:
{chr(10).join(feedback_rader)}

Baserat på denna feedback, generera 10 NYA namn som tar hänsyn till vad användaren vill ha mer och mindre av.
Generera INGA av de tidigare förslagen.
{variation_krav}

Svara ENDAST med en JSON-array med exakt 10 strängar, inga förklaringar:
["Namn1", "Namn2", ...]'''
    else:
        prompt = f'''Du är en expert på att skapa företagsnamn. Generera exakt 10 kreativa företagsnamn baserat på:
{kontext}
{variation_krav}{favoriter_tillagg}

Svara ENDAST med en JSON-array med exakt 10 strängar, inga förklaringar:
["Namn1", "Namn2", ...]'''

    print(f'[PROMPT]\n{prompt}', flush=True)

    try:
        import json as _json, re as _re
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        message = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=300,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = message.content[0].text.strip()
        match = _re.search(r'\[.*?\]', raw, _re.DOTALL)
        if not match:
            raise ValueError('Ingen JSON-array i svaret')
        namn = _json.loads(match.group())
        if not isinstance(namn, list):
            raise ValueError('Oväntat svar')
        return jsonify({'namn': namn[:10]})
    except Exception as e:
        print(f'[GENERERA] Fel: {e}', flush=True)
        return jsonify({'error': 'Namngenerering misslyckades, försök igen.'})

@app.route('/test_op')
def test_op():
    try:
        token = get_op_token()
        payload = {'domains': [{'name': 'fiskbularna', 'extension': 'se'}], 'with_price': True}
        r = requests.post(
            'https://api.openprovider.eu/v1beta/domains/check',
            headers={'Authorization': f'Bearer {token}'},
            json=payload,
            timeout=10,
        )
        data = r.json()
        if data.get('code') != 0:
            return jsonify({'error': data.get('desc', 'API-fel'), 'kod': data.get('code')})

        item = ((data.get('data') or {}).get('results') or [{}])[0]
        ledig = item.get('status') == 'free'
        price_block = (item.get('price') or {}).get('product') or {}
        return jsonify({
            'doman': 'fiskbularna.se',
            'ledig': ledig,
            'status': item.get('status'),
            'pris': price_block.get('price'),
            'valuta': price_block.get('currency'),
            'handle': _op_handle_cache['handle'],
        })
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/kop/<paket>')
@limiter.limit('5 per minute')
def kop(paket):
    if paket not in PAKET:
        return 'Ogiltigt paket', 400
    p = PAKET[paket]
    sid = get_session_id()
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'sek',
                    'product_data': {'name': f'Namnkoll {p["namn"]} — {p["tokens"]} tokens'},
                    'unit_amount': p['pris_ore'],
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=request.host_url + 'tack?stripe_session={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url,
            metadata={'session_id': sid, 'tokens': str(p['tokens'])},
        )
    except Exception as e:
        return f'Stripe-fel: {e}', 500
    resp = redirect(session.url, code=303)
    resp.set_cookie('sid', sid, max_age=60 * 60 * 24 * 365, samesite='Lax')
    return resp

KOPA_DOMAN_LANDING_HTML = '''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Registrera din domän — Namnverket</title>
    <meta name="description" content="Registrera din .se eller .com domän direkt. Snabb registrering, bra priser, ingen krångel.">
    <meta name="keywords" content="köp domän, registrera domän, billig domän Sverige, .se domän, köp .se, domänregistrering">
    <meta property="og:title" content="Registrera din domän — Namnverket">
    <meta property="og:description" content="Registrera din .se eller .com domän direkt. Snabb registrering, bra priser, ingen krångel.">
    <meta property="og:url" content="https://namnverket.se/kop-doman">
    <meta property="og:type" content="website">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://namnverket.se/kop-doman">
    <link rel="alternate" hreflang="sv" href="https://namnverket.se/kop-doman" />
    <meta property="og:image" content="https://namnverket.se/og-bild.svg">
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Product",
      "name": "Domänregistrering",
      "description": "Registrera .se och .com domäner direkt via Namnverket",
      "url": "https://namnverket.se/kop-doman",
      "offers": {
        "@type": "AggregateOffer",
        "lowPrice": "149",
        "highPrice": "499",
        "priceCurrency": "SEK",
        "offerCount": "4"
      }
    }
    </script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --svart: #0a0a0a;
            --text-sekunder: #6b6b6b;
            --text-tertiar: #a0a0a0;
            --border: rgba(0,0,0,0.08);
            --gron: #16a34a;
            --rod: #dc2626;
            --yta: #f9f9f8;
        }
        *, *::before, *::after { box-sizing: border-box; }
        body { font-family: \'Inter\', sans-serif; max-width: 580px; margin: 80px auto 120px; padding: 0 24px; color: var(--svart); }
        .logo { font-size: 11px; letter-spacing: 0.15em; color: var(--text-tertiar); margin-bottom: 2rem; font-weight: 400; }
        .back { font-size: 13px; color: var(--text-tertiar); text-decoration: none; display: inline-block; margin-bottom: 28px; }
        .back:hover { color: var(--svart); }
        h1 { font-size: 36px; font-weight: 500; letter-spacing: -0.02em; line-height: 1.15; margin-bottom: 10px; }
        .sub { font-size: 15px; color: var(--text-sekunder); margin-bottom: 36px; }
        .pris-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 36px; }
        .pris-kort {
            border: 0.5px solid var(--border); border-radius: 12px;
            padding: 20px; background: var(--yta);
        }
        .pris-kort .tld { font-size: 22px; font-weight: 500; margin-bottom: 4px; }
        .pris-kort .pris { font-size: 14px; color: var(--text-sekunder); }
        .pris-kort .ingår { font-size: 12px; color: var(--text-tertiar); margin-top: 10px; line-height: 1.6; }
        .sok-sektion label { display: block; font-size: 12px; color: var(--text-tertiar); letter-spacing: 0.04em; margin-bottom: 10px; }
        .sok-rad { display: flex; gap: 8px; }
        .sok-rad input {
            flex: 1; height: 48px; padding: 0 14px; font-size: 15px;
            font-family: \'Inter\', sans-serif; border: 1px solid var(--border);
            border-radius: 8px; outline: none; color: var(--svart); background: #fff;
        }
        .sok-rad input:focus { border-color: rgba(0,0,0,0.2); }
        .sok-rad input::placeholder { color: var(--text-tertiar); }
        .sok-btn {
            height: 48px; padding: 0 22px; background: var(--svart); color: #fff;
            border: none; border-radius: 8px; font-size: 14px; font-family: \'Inter\', sans-serif;
            font-weight: 500; cursor: pointer; white-space: nowrap;
        }
        .sok-btn:hover { background: #1a1a1a; }
        .sok-btn:disabled { background: var(--text-tertiar); cursor: not-allowed; }
        #doman-resultat { margin-top: 24px; }
        .res-rad {
            display: flex; justify-content: space-between; align-items: center;
            padding: 13px 0; border-bottom: 0.5px solid var(--border); font-size: 14px;
        }
        .res-ledig { color: var(--gron); font-weight: 500; }
        .res-tagen { color: var(--rod); }
        .res-laddar { color: var(--text-tertiar); }
        .kop-btn {
            border-radius: 999px; border: none; padding: 5px 16px; font-size: 12px;
            font-family: \'Inter\', sans-serif; background: var(--svart); color: #fff;
            cursor: pointer; text-decoration: none; display: inline-block; line-height: 24px;
        }
        .kop-btn:hover { background: #1a1a1a; }
        .pris-badge { font-size: 12px; color: var(--text-tertiar); margin-right: 10px; }
        .fel-text { font-size: 14px; color: var(--rod); padding-top: 12px; }
        @media (max-width: 600px) {
            body { padding: 0 16px; margin-top: 40px; }
            h1 { font-size: 28px; }
            input { font-size: 16px; }
            .pris-grid { grid-template-columns: 1fr; }
            .sok-rad { flex-direction: column; }
            .sok-btn { width: 100%; }
        }
    </style>
</head>
<body>
    <header>
        <p class="logo">NAMNVERKET</p>
    </header>
    <main>
        <a class="back" href="/">← tillbaka</a>
        <h1>Registrera din domän</h1>
        <p class="sub">Snabb registrering direkt hos registraren — din domän är aktiv inom minuter.</p>

        <div class="pris-grid">
            <div class="pris-kort">
                <div class="tld">.se</div>
                <div class="pris">från 149 kr/år</div>
                <div class="ingår">✓ Gratis DNS<br>✓ WHOIS-skydd<br>✓ Omedelbar aktivering</div>
            </div>
            <div class="pris-kort">
                <div class="tld">.com</div>
                <div class="pris">från 199 kr/år</div>
                <div class="ingår">✓ Gratis DNS<br>✓ WHOIS-skydd<br>✓ Omedelbar aktivering</div>
            </div>
        </div>

        <div class="sok-sektion">
            <label>SÖK EFTER EN DOMÄN</label>
            <div class="sok-rad">
                <input type="text" id="doman-input" placeholder="mittforetag.se" autocomplete="off" />
                <button class="sok-btn" id="sok-btn" onclick="kollaLedig()">Kolla tillgänglighet</button>
            </div>
        </div>
        <div id="doman-resultat"></div>
    </main>

    <script>
        var _input = document.getElementById(\'doman-input\');
        var _btn   = document.getElementById(\'sok-btn\');
        var _res   = document.getElementById(\'doman-resultat\');

        _input.addEventListener(\'keydown\', function(e) {
            if (e.key === \'Enter\') kollaLedig();
        });

        async function kollaLedig() {
            var val = _input.value.trim().toLowerCase();
            if (!val) return;
            if (!val.includes(\'.\')) val = val + \'.se\';
            _btn.disabled = true;
            _res.innerHTML = \'<p class="res-laddar">Kollar tillgänglighet...</p>\';
            try {
                var r = await fetch(\'/op_pris?doman=\' + encodeURIComponent(val));
                var d = await r.json();
                if (d.kundpris) {
                    _res.innerHTML =
                        \'<div class="res-rad">\' +
                        \'  <span><strong>\' + val + \'</strong> <span class="res-ledig">— ledig</span></span>\' +
                        \'  <span><span class="pris-badge">\' + d.kundpris + \' kr/år</span>\' +
                        \'  <a class="kop-btn" href="/kop-doman?doman=\' + encodeURIComponent(val) + \'">Köp →</a></span>\' +
                        \'</div>\';
                } else {
                    _res.innerHTML =
                        \'<div class="res-rad">\' +
                        \'  <span><strong>\' + val + \'</strong></span>\' +
                        \'  <span class="res-tagen">\' + (d.error || \'Ej tillgänglig\') + \'</span>\' +
                        \'</div>\';
                }
            } catch(e) {
                _res.innerHTML = \'<p class="fel-text">Något gick fel, försök igen.</p>\';
            }
            _btn.disabled = false;
        }
    </script>
</body>
</html>
'''

@app.route('/kop-doman')
@limiter.limit('5 per minute')
def kop_doman():
    doman = request.args.get('doman', '').strip().lower()
    if not doman:
        return render_template_string(KOPA_DOMAN_LANDING_HTML)
    if not _valider_doman(doman):
        return 'Ogiltig domän', 400
    sid = get_session_id()
    print(f'[KÖP-DOMÄN] raw email param: {request.args.get("email")}', flush=True)
    print(f'[KÖP-DOMÄN] raw nk_email cookie: {request.cookies.get("nk_email")!r}', flush=True)
    email = unquote(request.args.get('email', '')) or unquote(request.cookies.get('nk_email', '').strip())
    if email and not _valider_email(email):
        email = ''
    print(f'[KÖP-DOMÄN] decoded email: {email!r}', flush=True)
    try:
        _, kundpris, _ = hämta_pris(doman)
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            customer_email=email or None,
            line_items=[{
                'price_data': {
                    'currency': 'sek',
                    'product_data': {'name': f'Domänregistrering {doman} — 1 år'},
                    'unit_amount': kundpris * 100,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=request.host_url + 'tack-doman?stripe_session={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url,
            metadata={'doman': doman, 'session_id': sid, 'typ': 'doman'},
        )
    except Exception as e:
        print(f'[KOP_DOMAN] Fel: {e}', flush=True)
        return 'Betalning kunde inte skapas, försök igen.', 500
    resp = redirect(session.url, code=303)
    resp.set_cookie('sid', sid, max_age=60 * 60 * 24 * 365, samesite='Lax')
    return resp

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            print('[WEBHOOK] VARNING: STRIPE_WEBHOOK_SECRET ej konfigurerad — signatur verifieras ej', flush=True)
            body = request.get_json(silent=True)
            if not body:
                return 'Ogiltigt webhook-format', 400
            event = stripe.Event.construct_from(body, stripe.api_key)
    except Exception as e:
        print(f'[WEBHOOK] Fel vid parsning: {e}', flush=True)
        return 'Ogiltig webhook-förfrågan', 400

    print(f'[WEBHOOK] event.type={event["type"]}', flush=True)

    if event['type'] == 'checkout.session.completed':
        sess = event['data']['object']
        customer_email = getattr(sess, 'customer_email', None)
        print(f'[WEBHOOK] customer_email={customer_email}', flush=True)
        try:
            sid = sess.metadata['session_id']
        except (KeyError, AttributeError, TypeError):
            sid = None
        try:
            tokens = int(sess.metadata['tokens'])
        except (KeyError, AttributeError, TypeError, ValueError):
            tokens = 0
        stripe_sid = getattr(sess, 'id', '')
        print(f'[WEBHOOK] sid={sid} tokens={tokens} stripe_sid={stripe_sid}', flush=True)
        if sid and tokens and markera_betald(stripe_sid):
            add_tokens(sid, tokens)
            print(f'[WEBHOOK] Lade till {tokens} tokens för sid={sid}', flush=True)
        else:
            print(f'[WEBHOOK] Redan hanterad eller saknar data', flush=True)

    return '', 200

TACK_HTML = '''
<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Tack &mdash; Namnverket</title>
    <meta name="robots" content="noindex, nofollow">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; max-width: 580px; margin: 120px auto; padding: 0 24px; color: #0a0a0a; text-align: center; }
        .logo { font-size: 11px; letter-spacing: 0.15em; color: #a0a0a0; margin-bottom: 3rem; }
        h1 { font-size: 32px; font-weight: 500; letter-spacing: -0.02em; margin-bottom: 12px; }
        p { font-size: 15px; color: #6b6b6b; margin-bottom: 8px; }
        .email { font-size: 13px; color: #a0a0a0; margin-bottom: 32px; }
        a { display: inline-block; font-size: 14px; color: #0a0a0a; text-decoration: none; border-bottom: 0.5px solid rgba(0,0,0,0.2); padding-bottom: 2px; }
        a:hover { border-color: #0a0a0a; }
        @media (max-width: 600px) {
            body { padding: 0 16px; margin-top: 40px; }
            h1 { font-size: 28px; }
        }
    </style>
</head>
<body>
    <header><p class="logo">NAMNVERKET</p></header>
    <main>
    <h1>Tack!</h1>
    <p>{{ tokens }} tokens har lagts till{% if email %} på {{ email }}{% endif %}.</p>
    <p class="email">Du kan nu söka namn.</p>
    <a href="/">Tillbaka till sökningen →</a>
    </main>
</body>
</html>
'''

@app.route('/tack')
def tack():
    stripe_session_id = request.args.get('stripe_session', '').strip()
    tokens_tillagda = 0
    email = ''

    if stripe_session_id and markera_betald(stripe_session_id):
        try:
            sess = stripe.checkout.Session.retrieve(stripe_session_id)
            print(f'[TACK] payment_status={sess.payment_status} email={sess.customer_email} belopp={sess.amount_total}', flush=True)
            if sess.payment_status == 'paid':
                email = sess.customer_email or ''
                tokens_tillagda = BELOPP_TOKENS.get(getattr(sess, 'amount_total', 0), 0)
                if not tokens_tillagda:
                    try:
                        tokens_tillagda = int(sess.metadata['tokens'])
                    except (KeyError, AttributeError, TypeError, ValueError):
                        tokens_tillagda = 0
                user_key = email or get_session_id()
                if tokens_tillagda and user_key:
                    add_tokens(user_key, tokens_tillagda)
                    print(f'[TACK] Lade till {tokens_tillagda} tokens för {user_key}', flush=True)
        except Exception as e:
            print(f'[TACK] Stripe-fel: {e}', flush=True)

    resp = make_response(render_template_string(TACK_HTML, tokens=tokens_tillagda, email=email))
    if email:
        resp.set_cookie('nk_email', email, max_age=60 * 60 * 24 * 365, samesite='Lax')
    return resp

TACK_DOMAN_HTML = '''
<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{% if ok %}{{ doman }} är din{% else %}Betalning mottagen{% endif %} &mdash; Namnverket</title>
    <meta name="robots" content="noindex, nofollow">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; max-width: 580px; margin: 120px auto; padding: 0 24px; color: #0a0a0a; text-align: center; }
        .logo { font-size: 11px; letter-spacing: 0.15em; color: #a0a0a0; margin-bottom: 3rem; font-weight: 400; }
        h1 { font-size: 32px; font-weight: 500; letter-spacing: -0.02em; margin-bottom: 16px; }
        .doman { font-size: 22px; font-weight: 500; margin-bottom: 12px; }
        p { font-size: 15px; color: #6b6b6b; margin-bottom: 8px; }
        .epost { font-size: 13px; color: #a0a0a0; margin-bottom: 32px; }
        .varning { font-size: 13px; color: #d97706; margin-bottom: 24px; }
        a.hem { display: inline-block; font-size: 14px; color: #0a0a0a; text-decoration: none; border-bottom: 0.5px solid rgba(0,0,0,0.2); padding-bottom: 2px; }
        a.hem:hover { border-color: #0a0a0a; }
        @media (max-width: 600px) {
            body { padding: 0 16px; margin-top: 40px; }
            h1 { font-size: 28px; }
        }
    </style>
</head>
<body>
    <header><p class="logo">NAMNVERKET</p></header>
    <main>
    {% if ok %}
    <h1>Grattis!</h1>
    <p class="doman">{{ doman }}</p>
    <p>är nu registrerad och din.</p>
    {% if email %}<p class="epost">En bekräftelse skickas till {{ email }}.</p>{% endif %}
    {% else %}
    <h1>Betalning mottagen.</h1>
    {% if doman %}<p class="doman">{{ doman }}</p>{% endif %}
    {% if fel %}<p class="varning">{{ fel }}</p>{% endif %}
    <p class="epost">Vi kontaktar dig{% if email %} på {{ email }}{% endif %} angående domänregistreringen.</p>
    {% endif %}
    <a class="hem" href="/">Tillbaka till sökningen →</a>
    </main>
</body>
</html>
'''

@app.route('/tack-doman')
def tack_doman():
    stripe_session_id = request.args.get('stripe_session', '').strip()
    doman = ''
    email = ''
    ok = False
    fel = ''

    if not stripe_session_id:
        return redirect('/')

    nybetald = markera_betald(stripe_session_id)
    try:
        sess = stripe.checkout.Session.retrieve(stripe_session_id)
        print(f'[TACK_DOMAN] payment_status={sess.payment_status}', flush=True)
        if sess.payment_status == 'paid':
            email = getattr(sess, 'customer_email', None) or ''
            try:
                doman = sess.metadata['doman']
            except (KeyError, AttributeError, TypeError):
                doman = ''
            print(f'[TACK_DOMAN] doman={doman!r} email={email!r} nybetald={nybetald}', flush=True)

            if nybetald and doman:
                try:
                    namn, ext = doman.split('.', 1)
                    token = get_op_token()
                    handle = get_op_handle()
                    payload = {
                        'domain': {'name': namn, 'extension': ext},
                        'period': 1,
                        'owner_handle': handle,
                        'name_servers': [
                            {'name': 'ns1.openprovider.eu'},
                            {'name': 'ns2.openprovider.eu'},
                            {'name': 'ns3.openprovider.eu'},
                        ],
                    }
                    if ext == 'se':
                        payload['additional_data'] = {'iisse_acceptance': '1'}
                    r = requests.post(
                        'https://api.openprovider.eu/v1beta/domains',
                        headers={'Authorization': f'Bearer {token}'},
                        json=payload,
                        timeout=15
                    )
                    data = r.json()
                    print(f'[TACK_DOMAN] OP svar code={data.get("code")} desc={data.get("desc")}', flush=True)
                    op_id = str((data.get('data') or {}).get('id', ''))
                    fornyelse = (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')
                    if data.get('code') == 0:
                        ok = True
                    else:
                        fel = data.get('desc', 'Registreringsfel')
                    con = sqlite3.connect(DB)
                    con.execute(
                        'INSERT INTO "köpta_domäner" (email, doman, förnyelsedatum, openprovider_id) VALUES (?, ?, ?, ?)',
                        (email, doman, fornyelse, op_id)
                    )
                    con.commit()
                    con.close()
                except Exception as e:
                    print(f'[TACK_DOMAN] OP-fel: {e}', flush=True)
                    fel = str(e)
                    try:
                        con = sqlite3.connect(DB)
                        con.execute(
                            'INSERT INTO "köpta_domäner" (email, doman, förnyelsedatum, openprovider_id) VALUES (?, ?, ?, ?)',
                            (email, doman, '', '')
                        )
                        con.commit()
                        con.close()
                    except Exception:
                        pass
            elif not nybetald and doman and email:
                con = sqlite3.connect(DB)
                row = con.execute(
                    'SELECT id FROM "köpta_domäner" WHERE doman = ? AND email = ?',
                    (doman, email)
                ).fetchone()
                con.close()
                ok = row is not None
    except Exception as e:
        print(f'[TACK_DOMAN] Stripe-fel: {e}', flush=True)

    resp = make_response(render_template_string(TACK_DOMAN_HTML, ok=ok, doman=doman, email=email, fel=fel))
    if email:
        resp.set_cookie('nk_email', email, max_age=60 * 60 * 24 * 365, samesite='Lax')
    return resp

@app.route('/analysera_favoriter')
def analysera_favoriter():
    sid = get_session_id()
    if not deduct_tokens(sid, 2):
        return jsonify({'error': 'Du behöver fler tokens'}), 402
    con = sqlite3.connect(DB)
    rows = con.execute(
        'SELECT namn FROM sparade_namn WHERE email = ? ORDER BY skapad',
        (sid,)
    ).fetchall()
    con.close()
    if not rows:
        return jsonify({'error': 'Inga sparade namn'})
    namn_lista = [r[0] for r in rows]
    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    message = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=150,
        messages=[{
            'role': 'user',
            'content': (
                f'Analysera dessa företagsnamn och ge en genomsnittlig poäng 0-10 för hela samlingen: {", ".join(namn_lista)}. '
                f'Dimensioner: '
                f'1) internationellt (passar globalt, lätt att uttala i många länder) '
                f'2) modernt (nutida känsla, inte gammaldags) '
                f'3) kort (genomsnittlig längd — ≤5 bokstäver=10, 6-7=7, 8-9=5, 10+=2) '
                f'4) abstrakt (påhittat/abstrakt snarare än beskrivande) '
                f'5) nordiskt (skandinavisk känsla och klang) '
                f'Svara ENDAST med exakt detta JSON utan förklaringar: {{"internationellt":7,"modernt":8,"kort":5,"abstrakt":6,"nordiskt":4}}'
            )
        }]
    )
    import json as _json, re as _re
    raw = message.content[0].text.strip()
    match = _re.search(r'\{[^{}]*\}', raw, _re.DOTALL)
    if not match:
        return jsonify({'error': 'Kunde inte analysera'})
    try:
        data = _json.loads(match.group())
        return jsonify(data)
    except Exception:
        return jsonify({'error': 'Kunde inte tolka svar'})

_SIDA_CSS = '''
        *, *::before, *::after { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; max-width: 620px; margin: 80px auto 120px; padding: 0 24px; color: #0a0a0a; }
        .logo { font-size: 11px; letter-spacing: 0.15em; color: #a0a0a0; margin-bottom: 2rem; font-weight: 400; }
        .back { font-size: 13px; color: #a0a0a0; text-decoration: none; display: inline-block; margin-bottom: 28px; }
        .back:hover { color: #0a0a0a; }
        h1 { font-size: 32px; font-weight: 500; letter-spacing: -0.02em; line-height: 1.2; margin-bottom: 8px; }
        .sub { font-size: 15px; color: #6b6b6b; margin-bottom: 40px; }
        .sektion { margin-bottom: 48px; }
        .sektion-rubrik { font-size: 11px; letter-spacing: 0.12em; color: #a0a0a0; margin-bottom: 16px; font-weight: 400; }
        .rad-lista { list-style: none; padding: 0; margin: 0; }
        .rad-lista li { display: flex; justify-content: space-between; align-items: center; padding: 11px 0; border-bottom: 0.5px solid rgba(0,0,0,0.08); font-size: 14px; }
        .rad-lista li:first-child { border-top: 0.5px solid rgba(0,0,0,0.08); }
        .antal { font-size: 13px; color: #a0a0a0; }
        .pil-upp { color: #16a34a; font-size: 16px; margin-left: 6px; }
        .pil-ned { color: #dc2626; font-size: 16px; margin-left: 6px; }
        .pil-flat { color: #a0a0a0; font-size: 16px; margin-left: 6px; }
        .bar { height: 4px; background: #f0f0f0; border-radius: 2px; width: 80px; display: inline-block; vertical-align: middle; margin-left: 8px; }
        .bar-fill { height: 4px; background: #0a0a0a; border-radius: 2px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { text-align: left; font-size: 11px; letter-spacing: 0.08em; color: #a0a0a0; font-weight: 400; padding: 0 0 10px; border-bottom: 0.5px solid rgba(0,0,0,0.08); }
        td { padding: 11px 0; border-bottom: 0.5px solid rgba(0,0,0,0.08); vertical-align: top; color: #4a4a4a; }
        td:first-child { color: #0a0a0a; font-weight: 500; }
        .pris-cell { color: #16a34a; font-weight: 500; }
        .tom { font-size: 13px; color: #a0a0a0; padding: 16px 0; }
        .estimator { margin-top: 0; }
        .est-rad { display: flex; gap: 8px; }
        .est-input { flex: 1; height: 44px; padding: 0 12px; font-size: 14px; font-family: 'Inter', sans-serif; border: 0.5px solid rgba(0,0,0,0.15); border-radius: 8px; outline: none; color: #0a0a0a; background: #fff; }
        .est-input:focus { border-color: rgba(0,0,0,0.3); }
        .est-btn { height: 44px; padding: 0 20px; background: #0a0a0a; color: #fff; border: none; border-radius: 8px; font-size: 13px; font-family: 'Inter', sans-serif; font-weight: 500; cursor: pointer; white-space: nowrap; }
        .est-btn:hover { background: #1a1a1a; }
        .est-btn:disabled { background: #a0a0a0; cursor: not-allowed; }
        #est-resultat { margin-top: 16px; font-size: 14px; color: #4a4a4a; line-height: 1.7; }
        .est-varde { font-size: 20px; font-weight: 500; color: #0a0a0a; margin-bottom: 6px; }
        .fakta-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
        .fakta-ruta { border: 0.5px solid rgba(0,0,0,0.08); border-radius: 12px; padding: 18px 16px; background: #f9f9f8; }
        .fakta-varde { font-size: 17px; font-weight: 500; letter-spacing: -0.01em; margin-bottom: 6px; }
        .fakta-text { font-size: 12px; color: #6b6b6b; line-height: 1.55; }
        @media (max-width: 600px) { body { padding: 0 16px; margin-top: 40px; } h1 { font-size: 26px; } .est-rad { flex-direction: column; } input { font-size: 16px; } button { width: 100%; } .fakta-grid { grid-template-columns: 1fr; } }
'''

TRENDER_HTML = '''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Trender — Namnverket</title>
    <meta name="description" content="Se vilka branscher som är hetast just nu, vad som söks mest på Namnverket och nyregistrerade bolag i Sverige.">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://namnverket.se/trender">
    <link rel="alternate" hreflang="sv" href="https://namnverket.se/trender" />
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <style>''' + _SIDA_CSS + '''</style>
</head>
<body>
    <header><p class="logo">NAMNVERKET</p></header>
    <main>
    <a class="back" href="/">← tillbaka</a>
    <h1>Trender</h1>
    <p class="sub">Vad som rör sig just nu — branscher, sökningar och nyregistreringar.</p>

    <div class="sektion">
        <p class="sektion-rubrik">HETA BRANSCHER JUST NU &mdash; GOOGLE TRENDS SE (7 DAGAR)</p>
        {% if trender %}
        <ul class="rad-lista">
            {% for t in trender %}
            <li>
                <span>{{ t.namn }}{% if t.pil == '↑' %}<span class="pil-upp">↑</span>{% elif t.pil == '↓' %}<span class="pil-ned">↓</span>{% else %}<span class="pil-flat">→</span>{% endif %}</span>
                <span class="antal" style="display:flex;align-items:center;gap:8px;">
                    <span class="bar"><span class="bar-fill" style="width:{{ t.varde }}%"></span></span>
                    {{ t.varde }}/100
                </span>
            </li>
            {% endfor %}
        </ul>
        {% else %}
        <p class="tom">Trenddata uppdateras&hellip; (hämtas från Google Trends)</p>
        {% endif %}
    </div>

    <div class="sektion">
        <p class="sektion-rubrik">MEST SÖKTA PÅ NAMNVERKET (7 DAGAR)</p>
        {% if mest_sokta %}
        <ul class="rad-lista">
            {% for s in mest_sokta %}
            <li>
                <span><a href="/?namn={{ s.namn|urlencode }}" style="color:inherit;text-decoration:none;border-bottom:0.5px solid rgba(0,0,0,0.15);">{{ s.namn }}</a></span>
                <span class="antal">{{ s.antal }} sökningar</span>
            </li>
            {% endfor %}
        </ul>
        {% else %}
        <p class="tom">Inga sökningar loggade ännu &mdash; börja söka på <a href="/">startsidan</a>.</p>
        {% endif %}
    </div>

    <div class="sektion">
        <p class="sektion-rubrik">NYREGISTRERADE BOLAG I SVERIGE (30 DAGAR)</p>
        {% if nya_bolag %}
        <ul class="rad-lista">
            {% for b in nya_bolag %}
            <li>
                <span>{{ b.form }}</span>
                <span class="antal">{{ "{:,}".format(b.antal).replace(",", " ") }} st</span>
            </li>
            {% endfor %}
        </ul>
        {% else %}
        <p class="tom">Data inte tillgänglig.</p>
        {% endif %}
    </div>
    </main>
</body>
</html>'''

DOMANMARKNADEN_HTML = '''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Domänmarknaden — Namnverket</title>
    <meta name="description" content="Top 10 dyraste domäner som sålts globalt, marknadsfakta och en AI-driven prisestimator för din domän.">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://namnverket.se/domanmarknaden">
    <link rel="alternate" hreflang="sv" href="https://namnverket.se/domanmarknaden" />
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <style>''' + _SIDA_CSS + '''</style>
</head>
<body>
    <header><p class="logo">NAMNVERKET</p></header>
    <main>
    <a class="back" href="/">← tillbaka</a>
    <h1>Domänmarknaden</h1>
    <p class="sub">De dyraste domänerna som sålts — och vad din domän är värd.</p>

    <div class="sektion">
        <p class="sektion-rubrik">TOP 10 DYRASTE SÅLDA DOMÄNER GLOBALT (KÄLLA: DNJOURNAL / WIKIPEDIA)</p>
        <table>
            <thead><tr><th>Domän</th><th>Pris i SEK</th><th>År</th></tr></thead>
            <tbody>
            {% for d in domaner %}
            <tr>
                <td>{{ d.doman }}</td>
                <td class="pris-cell">{{ d.sek }}&nbsp;kr</td>
                <td style="color:#a0a0a0;">{{ d.ar }}</td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        <p style="font-size:11px;color:#a0a0a0;margin-top:10px;">Priser omräknade till SEK (×10.5). Källa: DNJournal &amp; Wikipedia.</p>
    </div>

    <div class="sektion">
        <p class="sektion-rubrik">MARKNADSFAKTA</p>
        <div class="fakta-grid">
            <div class="fakta-ruta">
                <div class="fakta-varde">170 000 kr</div>
                <div class="fakta-text">Genomsnittligt försäljningspris på andrahandsmarknaden 2025</div>
            </div>
            <div class="fakta-ruta">
                <div class="fakta-varde">4× på 3 år</div>
                <div class="fakta-text">.ai-domäner har fyrdubblats i värde sedan 2022 — driven av AI-boomen</div>
            </div>
            <div class="fakta-ruta">
                <div class="fakta-varde">Praktiskt taget borta</div>
                <div class="fakta-text">Kortaste .com-domänerna (1–3 tecken) är i princip osäljbara — alla är sedan länge tagna</div>
            </div>
        </div>
    </div>

    <div class="sektion estimator">
        <p class="sektion-rubrik">PRISESTIMATOR &mdash; VAD ÄR DIN DOMÄN VÄRD?</p>
        <div class="est-rad">
            <input class="est-input" type="text" id="est-input" placeholder="mittforetag.se" autocomplete="off" />
            <button class="est-btn" id="est-btn" onclick="estimera()">Estimera →</button>
            <span style="font-size:12px;color:#a0a0a0;white-space:nowrap;">1 token</span>
        </div>
        <div id="est-resultat"></div>
    </div>
    </main>

    <script>
        document.getElementById(\'est-input\').addEventListener(\'keydown\', function(e) {
            if (e.key === \'Enter\') estimera();
        });
        async function estimera() {
            var doman = document.getElementById(\'est-input\').value.trim().toLowerCase();
            if (!doman) return;
            var btn = document.getElementById(\'est-btn\');
            var res = document.getElementById(\'est-resultat\');
            btn.disabled = true;
            btn.textContent = \'Analyserar...\';
            res.innerHTML = \'<p style="color:#a0a0a0;font-size:13px;">Claude analyserar din domän&hellip;</p>\';
            try {
                var r = await fetch(\'/estimera-doman?doman=\' + encodeURIComponent(doman));
                var d = await r.json();
                if (r.status === 402 || (d.error && d.error.toLowerCase().includes(\'token\'))) {
                    res.innerHTML = \'<p style="font-size:13px;color:#6b6b6b;">Du behöver tokens för att estimera. <a href="/kop/bas" style="color:#0a0a0a;border-bottom:0.5px solid rgba(0,0,0,0.2);">Köp 50 tokens för 19 kr →</a></p>\';
                } else if (d.error) {
                    res.innerHTML = \'<p style="color:#dc2626;font-size:13px;">\' + d.error + \'</p>\';
                } else {
                    res.innerHTML = \'<div class="est-varde">\' + d.estimat + \'</div><p>\' + d.motivering + \'</p>\';
                }
            } catch(e) {
                res.innerHTML = \'<p style="color:#dc2626;font-size:13px;">Något gick fel, försök igen.</p>\';
            }
            btn.disabled = false;
            btn.textContent = \'Estimera →\';
        }
    </script>
</body>
</html>'''

@app.route('/trender')
def trender():
    return render_template_string(
        TRENDER_HTML,
        trender=_hämta_trender(),
        mest_sokta=_mest_sokta(),
        nya_bolag=_nya_bolag(),
    )

@app.route('/domanmarknaden')
def domanmarknaden():
    return render_template_string(DOMANMARKNADEN_HTML, domaner=_TOP_DOMANER)

@app.route('/estimera-doman')
@limiter.limit('5 per minute')
def estimera_doman():
    doman = _sanera_text(request.args.get('doman', '').strip().lower(), max_len=100)
    if not doman or '.' not in doman:
        return jsonify({'error': 'Ange en giltig domän, t.ex. mittforetag.se'})
    sid = get_session_id()
    if not deduct_tokens(sid, 1):
        return jsonify({'error': 'Du behöver fler tokens'}), 402
    delar = doman.rsplit('.', 1)
    namn_del = delar[0]
    tld = delar[1] if len(delar) > 1 else ''
    langd = len(namn_del)
    langd_betyg = {1: 'extremt kort (sällsynt premium)', 2: 'mycket kort (premium)', 3: 'kort (högt värde)',
                   4: 'kort (bra värde)', 5: 'mediumkort (vanlig premiumlängd)',
                   6: 'medium', 7: 'mediumlång'}.get(langd, f'lång ({langd} tecken, lägre värde)')
    tld_rang = {'.com': 'högsta (global standard)', '.ai': 'mycket hög (tech/AI-premium)',
                '.io': 'hög (tech-bransch)', '.se': 'hög (Sverige)', '.net': 'medium',
                '.org': 'medium', '.eu': 'mediumlåg'}.get('.' + tld, 'lägre (nisch-TLD)')

    pc_rad = ''
    try:
        pc_r = requests.get(f'https://pc.domains/api/?domain={doman}',
                            headers={'User-Agent': 'Namnverket/1.0'}, timeout=5)
        if pc_r.status_code == 200:
            pc_data = pc_r.json()
            pc_usd = pc_data.get('value') or pc_data.get('price') or pc_data.get('estimate')
            if pc_usd:
                pc_rad = f'pc.domains uppskattar värdet till: {pc_usd} USD\n'
    except Exception:
        pass

    try:
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=200,
            messages=[{'role': 'user', 'content':
                f'Du är en domänvärderingsexpert. Estimera marknadsvärdet för domänen "{doman}".\n'
                f'Fakta: namnlängd={langd} tecken ({langd_betyg}), TLD=.{tld} (rankning: {tld_rang}), '
                f'domännamnet är "{namn_del}".\n'
                + pc_rad +
                f'Svara BARA med JSON: {{"estimat":"X-Y kr","motivering":"1-2 meningar på svenska"}}\n'
                f'Viktigt: sätt realistiska SEK-priser (andrahandsmarknaden), '
                f'.se-domäner är billigare än .com globalt.'
            }]
        )
        raw = msg.content[0].text.strip()
        import json as _json
        m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if m:
            data = _json.loads(m.group())
            return jsonify({'estimat': data.get('estimat', '?'), 'motivering': data.get('motivering', '')})
    except Exception as e:
        print(f'[ESTIMERA] fel: {e}', flush=True)
    return jsonify({'error': 'Kunde inte estimera just nu, försök igen.'})

@app.route('/llms.txt')
def llms_txt():
    txt = '''# Namnverket
> Namnverket är Sveriges mest kompletta verktyg för att hitta och säkra ett företagsnamn.

Namnverket kontrollerar samtidigt om ett företagsnamn är ledigt hos Bolagsverket, om domänen är ledig (.se, .com, .io, .ai), om varumärket är registrerat hos PRV och EU, samt ger en AI-driven analys av namnets betydelse på andra språk och kulturella konnotationer.

## Tjänster
- Bolagsnamnskoll mot Bolagsverkets officiella register (3 miljoner bolag)
- Domänregistrering .se från 149 kr/år, .com från 169 kr/år
- Varumärkeskoll mot PRV (Sverige) och TMview (EU)
- Wayback Machine-historik på domäner
- AI-driven namnanalys — språk, kultur, uttal
- Namnkonfigurator — skräddarsydda namnförslag baserat på bransch och känsla
- Slumpgenerator — slumpa fram ett företagsnamn

## Priser
- Namnkoll: gratis
- Tokens för AI-analys: 19-99 kr
- Domänregistrering .se: 149 kr/år
- Domänregistrering .com: 169 kr/år

## Målgrupp
Svenska entreprenörer, startups och småföretagare som ska starta eller byta namn på ett företag.

## Kontakt
namnverket.se'''
    return app.response_class(txt, mimetype='text/plain')

@app.route('/og-bild.svg')
def og_bild():
    svg = '''<svg width="1200" height="630" xmlns="http://www.w3.org/2000/svg">
  <rect width="1200" height="630" fill="#f9f9f8"/>
  <text x="80" y="220" font-family="'Inter',system-ui,sans-serif" font-size="80" font-weight="500" fill="#0a0a0a" letter-spacing="-2">Namnverket</text>
  <text x="80" y="300" font-family="'Inter',system-ui,sans-serif" font-size="30" fill="#6b6b6b">Hitta ett namn som faktiskt är ledigt.</text>
  <text x="80" y="370" font-family="'Inter',system-ui,sans-serif" font-size="22" fill="#a0a0a0">Bolagsverket · Domäner · Varumärken · AI-analys</text>
  <rect x="80" y="430" width="160" height="2" fill="#0a0a0a" opacity="0.08"/>
  <text x="80" y="480" font-family="'Inter',system-ui,sans-serif" font-size="18" fill="#a0a0a0" letter-spacing="2">NAMNVERKET.SE</text>
</svg>'''
    return app.response_class(svg, mimetype='image/svg+xml')

_CONTENT_HEAD = '''    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">'''

_CONTENT_CSS = '''        *, *::before, *::after { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; max-width: 620px; margin: 80px auto 120px; padding: 0 24px; color: #0a0a0a; }
        .logo { font-size: 11px; letter-spacing: 0.15em; color: #a0a0a0; margin-bottom: 2rem; font-weight: 400; }
        nav.bc { font-size: 12px; color: #a0a0a0; margin-bottom: 28px; }
        nav.bc a { color: #a0a0a0; text-decoration: none; }
        nav.bc a:hover { color: #0a0a0a; }
        nav.bc span { margin: 0 6px; }
        h1 { font-size: 36px; font-weight: 500; letter-spacing: -0.02em; line-height: 1.2; margin-bottom: 14px; }
        .ingress { font-size: 16px; color: #6b6b6b; line-height: 1.7; margin-bottom: 40px; border-bottom: 0.5px solid rgba(0,0,0,0.08); padding-bottom: 28px; }
        h2 { font-size: 18px; font-weight: 500; letter-spacing: -0.01em; margin: 36px 0 10px; }
        p { font-size: 15px; color: #4a4a4a; line-height: 1.75; margin-bottom: 16px; }
        a.intern { color: #0a0a0a; border-bottom: 0.5px solid rgba(0,0,0,0.2); text-decoration: none; padding-bottom: 1px; }
        a.intern:hover { border-color: #0a0a0a; }
        .cta { display: inline-block; margin-top: 32px; height: 48px; line-height: 48px; padding: 0 28px; background: #0a0a0a; color: #fff; border-radius: 8px; font-size: 14px; font-weight: 500; text-decoration: none; }
        .cta:hover { background: #1a1a1a; }
        footer.side { margin-top: 64px; padding-top: 24px; border-top: 0.5px solid rgba(0,0,0,0.08); font-size: 13px; color: #a0a0a0; }
        footer.side a { color: #a0a0a0; text-decoration: none; border-bottom: 0.5px solid rgba(0,0,0,0.15); }
        @media (max-width: 600px) { body { padding: 0 16px; margin-top: 40px; } h1 { font-size: 28px; } }'''

KOLLA_FORETAGSNAMN_HTML = f'''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Kolla om ett företagsnamn är ledigt — Namnverket</title>
    <meta name="description" content="Lär dig hur du kollar om ett företagsnamn är ledigt hos Bolagsverket, domäner och varumärken — gratis och på sekunder.">
    <meta property="og:title" content="Kolla om ett företagsnamn är ledigt — Namnverket">
    <meta property="og:description" content="Gratis namnkoll mot Bolagsverket, domäner och PRV i ett slag.">
    <meta property="og:url" content="https://namnverket.se/kolla-foretagsnamn">
    <meta property="og:type" content="article">
    <meta property="og:image" content="https://namnverket.se/og-bild.svg">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://namnverket.se/kolla-foretagsnamn">
    <link rel="alternate" hreflang="sv" href="https://namnverket.se/kolla-foretagsnamn" />
    <script type="application/ld+json">
    {{"@context":"https://schema.org","@type":"Article","headline":"Kolla om ett företagsnamn är ledigt","description":"Lär dig hur du snabbt kontrollerar om ett företagsnamn är ledigt hos Bolagsverket, domäner och varumärken.","url":"https://namnverket.se/kolla-foretagsnamn","publisher":{{"@type":"Organization","name":"Namnverket","url":"https://namnverket.se"}}}}
    </script>
    <script type="application/ld+json">
    {{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{{"@type":"ListItem","position":1,"name":"Namnverket","item":"https://namnverket.se"}},{{"@type":"ListItem","position":2,"name":"Kolla företagsnamn","item":"https://namnverket.se/kolla-foretagsnamn"}}]}}
    </script>
{_CONTENT_HEAD}
    <style>
{_CONTENT_CSS}
    </style>
</head>
<body>
    <header><p class="logo">NAMNVERKET</p></header>
    <main>
        <nav class="bc" aria-label="Brödsmulor">
            <a href="/">Namnverket</a><span>›</span><span>Kolla företagsnamn</span>
        </nav>
        <h1>Kolla om ett företagsnamn är ledigt</h1>
        <p class="ingress">Att starta ett företag börjar med rätt namn — men att hitta ett som faktiskt är ledigt kan vara svårare än man tror. Namnverket kontrollerar automatiskt Bolagsverkets register, domäntillgänglighet och varumärkesskydd i ett enda sökning, helt gratis.</p>

        <h2>Vad kontrolleras när du söker?</h2>
        <p>När du skriver in ett namn på Namnverket sker tre kontroller parallellt:</p>
        <p><strong>Bolagsverkets register</strong> — Vi söker mot databasen med över 3 miljoner aktiva och historiska bolag i Sverige. Ett identiskt eller förväxlingsbart namn kan neka din bolagsregistrering.</p>
        <p><strong>Domäntillgänglighet</strong> — Vi kollar om .se, .com, .io och .ai-domänerna är lediga. En ledig domän kan du registrera direkt via Namnverket för 149–199 kr/år.</p>
        <p><strong>Varumärkesregistret</strong> — Vi söker hos PRV (Sverige) och TMview (EU) om namnet är skyddat som varumärke inom din tänkta bransch.</p>

        <h2>Hur gör du namnkollet?</h2>
        <p>Det är enkelt: skriv in ditt önskade namn i sökfältet på <a href="/" class="intern">Namnverkets startsida</a>. Resultaten visas inom några sekunder. Du behöver inte skapa ett konto eller betala något — grundkollet är alltid gratis.</p>
        <p>För en djupare analys — vad namnet betyder på andra språk, hur det upplevs kulturellt och hur lätt det är att uttala internationellt — kan du använda AI-analysen. Den kostar tokens som du köper i förväg för 19–99 kr.</p>

        <h2>Vad händer om namnet är taget?</h2>
        <p>Om ett namn redan finns registrerat hos Bolagsverket visas det tydligt. Det betyder inte att du inte kan använda ett liknande namn — Bolagsverkets regler handlar om förväxlingsbarhet, och ett tillräckligt distinkt namn kan godkännas ändå. Namnverkets <a href="/generator" class="intern">Namnkonfigurator</a> kan hjälpa dig generera varianter.</p>

        <h2>Tips för att hitta ett ledigt namn</h2>
        <p>Testa kombinationer av ord, lägg till ett prefix eller suffix, eller prova ett helt påhittat ord. Kortare namn (4–7 bokstäver) är svårare att hitta lediga men enklare att minnas. Använd <a href="/generator" class="intern">Namnkonfiguratorn</a> eller <a href="/generator" class="intern">Slumpgeneratorn</a> för inspiration.</p>

        <a href="/" class="cta">Kolla ett namn nu →</a>
    </main>
    <footer class="side">
        <a href="/">Tillbaka till söket</a> &nbsp;·&nbsp;
        <a href="/registrera-doman-se">Registrera .se-domän</a> &nbsp;·&nbsp;
        <a href="/vad-ar-ett-varumarke">Om varumärken</a>
    </footer>
</body>
</html>
'''

REGISTRERA_DOMAN_HTML = f'''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Registrera en .se-domän — Namnverket</title>
    <meta name="description" content="Registrera din .se-domän direkt för 149 kr/år. Snabb aktivering, gratis DNS och WHOIS-skydd ingår. Steg-för-steg guide.">
    <meta property="og:title" content="Registrera en .se-domän — Namnverket">
    <meta property="og:description" content="Registrera din .se-domän för 149 kr/år. Snabb aktivering, DNS och WHOIS-skydd ingår.">
    <meta property="og:url" content="https://namnverket.se/registrera-doman-se">
    <meta property="og:type" content="article">
    <meta property="og:image" content="https://namnverket.se/og-bild.svg">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://namnverket.se/registrera-doman-se">
    <link rel="alternate" hreflang="sv" href="https://namnverket.se/registrera-doman-se" />
    <script type="application/ld+json">
    {{"@context":"https://schema.org","@type":"Article","headline":"Registrera en .se-domän","description":"Guide till att registrera en .se-domän i Sverige. Pris, process och vad som ingår.","url":"https://namnverket.se/registrera-doman-se","publisher":{{"@type":"Organization","name":"Namnverket","url":"https://namnverket.se"}}}}
    </script>
    <script type="application/ld+json">
    {{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{{"@type":"ListItem","position":1,"name":"Namnverket","item":"https://namnverket.se"}},{{"@type":"ListItem","position":2,"name":"Registrera .se-domän","item":"https://namnverket.se/registrera-doman-se"}}]}}
    </script>
{_CONTENT_HEAD}
    <style>
{_CONTENT_CSS}
    </style>
</head>
<body>
    <header><p class="logo">NAMNVERKET</p></header>
    <main>
        <nav class="bc" aria-label="Brödsmulor">
            <a href="/">Namnverket</a><span>›</span><span>Registrera .se-domän</span>
        </nav>
        <h1>Registrera en .se-domän</h1>
        <p class="ingress">En .se-domän är din adress på internet och signalerar att du är ett svenskt företag — vilket bygger förtroende hos svenska kunder. På Namnverket registrerar du din .se-domän direkt för 149 kr/år, utan krångel.</p>

        <h2>Vad är en .se-domän?</h2>
        <p>.se är den nationella toppdomänen för Sverige, administrerad av Internetstiftelsen. Det finns drygt 2 miljoner registrerade .se-domäner — men det finns fortfarande gott om lediga varianter. En .se-domän signalerar lokal närvaro och bygger förtroende bland svenska konsumenter och företag.</p>
        <p>För internationella satsningar kompletteras .se ofta med .com. På Namnverket kan du kolla och köpa båda på samma ställe.</p>

        <h2>Pris och vad som ingår</h2>
        <p>En .se-domän kostar <strong>149 kr per år</strong> via Namnverket. I priset ingår:</p>
        <p>✓ &nbsp;Gratis DNS-hantering &nbsp;&nbsp; ✓ &nbsp;WHOIS-skydd (dina kontaktuppgifter döljs) &nbsp;&nbsp; ✓ &nbsp;Omedelbar aktivering &nbsp;&nbsp; ✓ &nbsp;Automatisk förnyelse</p>
        <p>Jämfört med stora domänregistratorer är Namnverkets prissättning transparent — inga dolda förnyelseavgifter eller lock-in.</p>

        <h2>Hur registrerar du din .se-domän?</h2>
        <p>1. Sök efter din önskade domän på <a href="/" class="intern">Namnverkets startsida</a> eller på <a href="/kop-doman" class="intern">domänregistreringssidan</a>.</p>
        <p>2. Om domänen är ledig visas priset direkt. Klicka "Köp" och slutför betalningen med kort.</p>
        <p>3. Domänen är registrerad och aktiv inom några minuter. Du får en bekräftelse via e-post.</p>
        <p>Registreringen hanteras via vår ackrediterade domänregistrar och du är domänens officiella innehavare från dag ett.</p>

        <h2>.se eller .com — vilket ska du välja?</h2>
        <p>Riktar du dig primärt till svenska kunder? Välj .se — det är professionellt, lokalt och bygger förtroende. Planerar du en internationell satsning från start? Komplettera med .com. Många väljer att registrera båda för att skydda sitt varumärke och undvika att konkurrenter tar den andra varianten.</p>

        <a href="/kop-doman" class="cta">Kolla din domän nu →</a>
    </main>
    <footer class="side">
        <a href="/">Tillbaka till söket</a> &nbsp;·&nbsp;
        <a href="/kolla-foretagsnamn">Kolla företagsnamn</a> &nbsp;·&nbsp;
        <a href="/vad-ar-ett-varumarke">Om varumärken</a>
    </footer>
</body>
</html>
'''

VAD_AR_VARUMARKE_HTML = f'''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Vad är ett varumärke och hur skyddar du ditt? — Namnverket</title>
    <meta name="description" content="Lär dig vad ett varumärke är, varför du bör registrera det hos PRV och hur du kollar om ditt namn redan är skyddat — gratis via Namnverket.">
    <meta property="og:title" content="Vad är ett varumärke? — Namnverket">
    <meta property="og:description" content="Guide till varumärkesskydd i Sverige — PRV, kostnad och hur du kollar om ditt namn är ledigt.">
    <meta property="og:url" content="https://namnverket.se/vad-ar-ett-varumarke">
    <meta property="og:type" content="article">
    <meta property="og:image" content="https://namnverket.se/og-bild.svg">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://namnverket.se/vad-ar-ett-varumarke">
    <link rel="alternate" hreflang="sv" href="https://namnverket.se/vad-ar-ett-varumarke" />
    <script type="application/ld+json">
    {{"@context":"https://schema.org","@type":"Article","headline":"Vad är ett varumärke och hur skyddar du ditt?","description":"Guide till varumärkesskydd i Sverige. Vad PRV är, vad det kostar och hur du kontrollerar om ditt namn är ledigt.","url":"https://namnverket.se/vad-ar-ett-varumarke","publisher":{{"@type":"Organization","name":"Namnverket","url":"https://namnverket.se"}}}}
    </script>
    <script type="application/ld+json">
    {{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{{"@type":"ListItem","position":1,"name":"Namnverket","item":"https://namnverket.se"}},{{"@type":"ListItem","position":2,"name":"Vad är ett varumärke?","item":"https://namnverket.se/vad-ar-ett-varumarke"}}]}}
    </script>
{_CONTENT_HEAD}
    <style>
{_CONTENT_CSS}
    </style>
</head>
<body>
    <header><p class="logo">NAMNVERKET</p></header>
    <main>
        <nav class="bc" aria-label="Brödsmulor">
            <a href="/">Namnverket</a><span>›</span><span>Vad är ett varumärke?</span>
        </nav>
        <h1>Vad är ett varumärke och hur skyddar du ditt?</h1>
        <p class="ingress">Ett varumärke ger dig ensamrätt till ditt företagsnamn eller logotyp inom en viss bransch — och är ett av de viktigaste juridiska skydden du kan ge ditt företag. Här förklarar vi vad ett varumärke är, varför det är viktigt och hur du kontrollerar om ditt namn är ledigt.</p>

        <h2>Vad är ett varumärke?</h2>
        <p>Ett varumärke är ett kännetecken — ett namn, en logotyp, ett ljud eller en kombination — som identifierar dina varor eller tjänster och skiljer dem från andras. I Sverige registreras varumärken hos <strong>PRV (Patent- och registreringsverket)</strong>. EU-gemensamma varumärken registreras hos EUIPO och syns i TMview-databasen.</p>
        <p>Till skillnad från ett bolagsnamn (som enbart skyddar din juridiska entitet) ger ett varumärke dig rätt att hindra andra från att använda ett förväxlingsbart tecken i kommersiell verksamhet, även om deras bolag heter något annat.</p>

        <h2>Varför ska du registrera ditt varumärke?</h2>
        <p>Utan registrering har du visst skydd genom inarbetning — men det kräver att du kan bevisa att ditt märke är känt på marknaden, vilket är dyrt och svårt. Ett registrerat varumärke ger dig:</p>
        <p>✓ &nbsp;Ensamrätt i hela Sverige (eller EU med EU-märke) &nbsp;&nbsp; ✓ &nbsp;Rätt att agera mot intrång utan att behöva bevisa inarbetning &nbsp;&nbsp; ✓ &nbsp;Möjlighet att licensiera eller sälja varumärket &nbsp;&nbsp; ✓ &nbsp;Starkare position vid domäntvister</p>

        <h2>Vad kostar det att registrera hos PRV?</h2>
        <p>En varumärkesansökan hos PRV kostar <strong>2 500 kr för en varuklass</strong> (online-ansökan). Varje extra klass kostar ytterligare 2 000 kr. Skyddstiden är 10 år och kan sedan förnyas. EU-varumärke via EUIPO kostar från 850 euro och ger skydd i alla EU:s medlemsstater.</p>
        <p>Jämfört med kostnaden att driva en varumärkestvist i efterhand är registreringen en modest investering för de flesta företag.</p>

        <h2>Kontrollera om ditt varumärke är ledigt</h2>
        <p>Innan du ansöker bör du kontrollera att ingen annan redan har ett liknande märke i din bransch. <a href="/" class="intern">Namnverket kontrollerar automatiskt</a> mot PRV:s register och TMview (EU) när du söker på ett namn — gratis och på sekunder. Om varumärket är taget visas det tydligt, och du kan prova varianter direkt.</p>
        <p>Observera att Namnverkets koll är en snabbsökning — en fullständig varumärkessökning inför ansökan bör göras direkt i PRV:s databas eller av ett ombud.</p>

        <a href="/" class="cta">Kolla ditt namn nu →</a>
    </main>
    <footer class="side">
        <a href="/">Tillbaka till söket</a> &nbsp;·&nbsp;
        <a href="/kolla-foretagsnamn">Kolla företagsnamn</a> &nbsp;·&nbsp;
        <a href="/registrera-doman-se">Registrera .se-domän</a>
    </footer>
</body>
</html>
'''

@app.route('/kolla-foretagsnamn')
def kolla_foretagsnamn():
    return KOLLA_FORETAGSNAMN_HTML

@app.route('/registrera-doman-se')
def registrera_doman_se():
    return REGISTRERA_DOMAN_HTML

@app.route('/vad-ar-ett-varumarke')
def vad_ar_ett_varumarke():
    return VAD_AR_VARUMARKE_HTML

_MARKETPLACE_CSS = _SIDA_CSS + '''
        .form-group { margin-bottom: 20px; }
        label { display: block; font-size: 12px; letter-spacing: 0.08em; color: #a0a0a0; margin-bottom: 6px; font-weight: 400; }
        .form-input { width: 100%; height: 44px; padding: 0 12px; font-size: 14px; font-family: 'Inter', sans-serif; border: 0.5px solid rgba(0,0,0,0.15); border-radius: 8px; outline: none; color: #0a0a0a; background: #fff; }
        .form-input:focus { border-color: rgba(0,0,0,0.3); }
        textarea.form-input { height: 88px; padding: 10px 12px; resize: vertical; }
        .submit-btn { height: 48px; padding: 0 28px; background: #0a0a0a; color: #fff; border: none; border-radius: 8px; font-size: 14px; font-family: 'Inter', sans-serif; font-weight: 500; cursor: pointer; width: 100%; }
        .submit-btn:hover { background: #1a1a1a; }
        .info-lista { list-style: none; padding: 0; margin: 24px 0 0; }
        .info-lista li { font-size: 13px; color: #6b6b6b; padding: 7px 0; border-bottom: 0.5px solid rgba(0,0,0,0.06); display: flex; align-items: center; gap: 10px; }
        .info-lista li::before { content: "–"; color: #a0a0a0; }
        .fel-msg { color: #dc2626; font-size: 13px; margin-top: 12px; }
        .ok-msg { color: #16a34a; font-size: 14px; margin-top: 16px; padding: 14px; background: #f0fdf4; border-radius: 8px; }
        .sok-rad { display: flex; gap: 8px; margin-bottom: 24px; }
        .sok-input { flex: 1; height: 40px; padding: 0 12px; font-size: 14px; font-family: 'Inter', sans-serif; border: 0.5px solid rgba(0,0,0,0.12); border-radius: 8px; outline: none; }
        .sort-select { height: 40px; padding: 0 10px; font-size: 13px; font-family: 'Inter', sans-serif; border: 0.5px solid rgba(0,0,0,0.12); border-radius: 8px; outline: none; background: #fff; cursor: pointer; }
        .kop-btn { display: inline-block; padding: 5px 14px; background: #0a0a0a; color: #fff; border-radius: 999px; font-size: 12px; font-family: 'Inter', sans-serif; font-weight: 500; text-decoration: none; white-space: nowrap; }
        .kop-btn:hover { background: #1a1a1a; }
        .tom-msg { font-size: 14px; color: #a0a0a0; padding: 24px 0; text-align: center; }
        .paginering { display: flex; gap: 8px; margin-top: 24px; justify-content: center; }
        .sid-btn { padding: 6px 14px; border: 0.5px solid rgba(0,0,0,0.15); border-radius: 999px; font-size: 13px; font-family: 'Inter', sans-serif; background: none; cursor: pointer; }
        .sid-btn.aktiv { background: #0a0a0a; color: #fff; border-color: #0a0a0a; }
        .sid-btn:hover:not(.aktiv) { background: #f0f0f0; }
        .besk-cell { font-size: 12px; color: #6b6b6b; }
'''

SALJ_HTML = '''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Sälj din domän — Namnverket</title>
    <meta name="description" content="Lista din domän till salu på Namnverket. Gratis att lista, 10% provision vid försäljning.">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://namnverket.se/salj">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <style>''' + _MARKETPLACE_CSS + '''</style>
</head>
<body>
    <header><p class="logo">NAMNVERKET</p></header>
    <main>
    <a href="/" class="back">← tillbaka</a>
    <h1>Sälj din domän</h1>
    <p class="sub">Lista din domän på marknadsplatsen. Köpare hittar den direkt när de söker på Namnverket.</p>

    <div id="formular">
        <div class="form-group">
            <label>DOMÄNNAMN</label>
            <input type="text" id="doman" class="form-input" placeholder="t.ex. mittforetag.se" autocomplete="off" />
        </div>
        <div class="form-group">
            <label>DITT PRIS (SEK)</label>
            <input type="number" id="pris" class="form-input" placeholder="t.ex. 5000" min="100" />
        </div>
        <div class="form-group">
            <label>DIN E-POST</label>
            <input type="email" id="email" class="form-input" placeholder="du@exempel.se" />
        </div>
        <div class="form-group">
            <label>BESKRIVNING <span style="font-size:11px;color:#c0c0c0;">(valfri)</span></label>
            <textarea id="beskrivning" class="form-input" placeholder="Varför är detta ett bra namn?"></textarea>
        </div>
        <button class="submit-btn" onclick="listaDoMan()">Lista domän →</button>
        <p id="fel" class="fel-msg" style="display:none;"></p>
        <p id="ok" class="ok-msg" style="display:none;"></p>
    </div>

    <div id="verifiering-box" style="display:none;">
        <p style="font-size:15px;font-weight:500;margin-bottom:8px;">Din domän behöver verifieras innan den listas.</p>
        <p style="font-size:14px;color:#6b6b6b;margin-bottom:20px;">Lägg till följande TXT-post i din DNS:</p>
        <div style="background:#f9f9f8;border-radius:8px;padding:16px 18px;margin-bottom:20px;font-size:13px;">
            <div><span style="color:#a0a0a0;">Namn:</span> @ (eller <span id="ver-doman"></span>)</div>
            <div style="margin-top:6px;"><span style="color:#a0a0a0;">Typ:</span> TXT</div>
            <div style="margin-top:6px;"><span style="color:#a0a0a0;">Värde:</span> <code id="ver-kod" style="font-family:monospace;background:#efefef;padding:2px 6px;border-radius:4px;user-select:all;"></code></div>
        </div>
        <p style="font-size:13px;color:#a0a0a0;margin-bottom:20px;">När du lagt till posten (kan ta 5–60 min) — klicka Verifiera nedan.</p>
        <button class="submit-btn" id="ver-btn" onclick="verifieraNu()">Verifiera nu →</button>
        <p id="ver-msg" style="display:none;margin-top:12px;font-size:13px;"></p>
    </div>

    <ul class="info-lista">
        <li>Vi tar 10% provision vid försäljning</li>
        <li>Överlåtelse sker automatiskt via Openprovider</li>
        <li>Gratis att lista — inga upfront-kostnader</li>
    </ul>
    </main>

    <script>
        var _pendingDoman = '';
        var _pendingKod   = '';

        async function listaDoMan() {
            var doman = document.getElementById('doman').value.trim().toLowerCase();
            var pris  = parseInt(document.getElementById('pris').value, 10);
            var email = document.getElementById('email').value.trim();
            var besk  = document.getElementById('beskrivning').value.trim();
            var felEl = document.getElementById('fel');
            felEl.style.display = 'none';
            if (!doman || !doman.includes('.')) { felEl.textContent = 'Ange ett giltigt domännamn.'; felEl.style.display = 'block'; return; }
            if (!pris || pris < 100)            { felEl.textContent = 'Priset måste vara minst 100 kr.'; felEl.style.display = 'block'; return; }
            if (!email || !email.includes('@'))  { felEl.textContent = 'Ange en giltig e-postadress.'; felEl.style.display = 'block'; return; }
            var btn = document.querySelector('.submit-btn');
            btn.disabled = true; btn.textContent = 'sparar...';
            try {
                var r = await fetch('/salj', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({doman: doman, pris: pris, email: email, beskrivning: besk})
                });
                var d = await r.json();
                if (d.ok && d.pending) {
                    _pendingDoman = d.doman;
                    _pendingKod   = d.verify_code;
                    visaVerifiering(d.doman, d.verify_code);
                } else {
                    felEl.textContent = d.error || 'Något gick fel, försök igen.';
                    felEl.style.display = 'block';
                    btn.disabled = false; btn.textContent = 'Lista domän →';
                }
            } catch(e) {
                felEl.textContent = 'Nätverksfel — försök igen.';
                felEl.style.display = 'block';
                btn.disabled = false; btn.textContent = 'Lista domän →';
            }
        }

        function visaVerifiering(doman, kod) {
            document.getElementById('formular').style.display = 'none';
            document.querySelector('.info-lista').style.display = 'none';
            var box = document.getElementById('verifiering-box');
            box.style.display = 'block';
            document.getElementById('ver-doman').textContent = doman;
            document.getElementById('ver-kod').textContent = kod;
        }

        async function verifieraNu() {
            var btn = document.getElementById('ver-btn');
            var msg = document.getElementById('ver-msg');
            btn.disabled = true; btn.textContent = 'kollar DNS...';
            msg.style.display = 'none';
            try {
                var r = await fetch('/verifiera/' + encodeURIComponent(_pendingDoman));
                var d = await r.json();
                if (d.ok) {
                    btn.style.display = 'none';
                    msg.className = 'ok-msg';
                    msg.textContent = '✓ ' + _pendingDoman + ' är nu verifierad och listad på Namnverket!';
                    msg.style.display = 'block';
                } else {
                    msg.className = 'fel-msg';
                    msg.textContent = d.error || 'Verifiering misslyckades.';
                    msg.style.display = 'block';
                    btn.disabled = false; btn.textContent = 'Verifiera nu →';
                }
            } catch(e) {
                msg.className = 'fel-msg';
                msg.textContent = 'Nätverksfel — försök igen.';
                msg.style.display = 'block';
                btn.disabled = false; btn.textContent = 'Verifiera nu →';
            }
        }
    </script>
</body>
</html>
'''

MARKNADSPLATS_HTML = '''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Domänmarknadsplats — Namnverket</title>
    <meta name="description" content="Köp och sälj domäner på Namnverkets marknadsplats. Hitta lediga domäner till salu från privatpersoner och företag.">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://namnverket.se/marknadsplats">
    <link rel="alternate" hreflang="sv" href="https://namnverket.se/marknadsplats" />
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <style>''' + _MARKETPLACE_CSS + '''</style>
</head>
<body>
    <header><p class="logo">NAMNVERKET</p></header>
    <main>
    <a href="/" class="back">← tillbaka</a>
    <h1>Domänmarknadsplats</h1>
    <p class="sub">Domäner till salu — listade av privatpersoner och företag.</p>

    <div class="sok-rad">
        <input type="text" id="sok" class="sok-input" placeholder="sök domän..." oninput="filteraOchVisa()" />
        <select id="sort" class="sort-select" onchange="filteraOchVisa()">
            <option value="ny">nyast</option>
            <option value="billigast">billigast</option>
            <option value="dyrast">dyrast</option>
        </select>
    </div>

    <table>
        <thead><tr><th>DOMÄN</th><th>PRIS</th><th>BESKRIVNING</th><th></th></tr></thead>
        <tbody id="tabell-body"></tbody>
    </table>
    <p id="tom-msg" class="tom-msg" style="display:none;">Inga domäner matchar sökningen.</p>
    <div class="paginering" id="paginering"></div>

    <p style="margin-top:40px;font-size:13px;color:#a0a0a0;">
        Har du en domän att sälja? <a href="/salj" style="color:#0a0a0a;border-bottom:0.5px solid rgba(0,0,0,0.2);">Lista den gratis →</a>
    </p>
    </main>

    <script>
        var alla = {{ domaner | tojson }};
        var PER_SIDA = 20;
        var sida = 1;

        function filteraOchVisa() {
            sida = 1;
            visa();
        }

        function visa() {
            var sok   = document.getElementById('sok').value.trim().toLowerCase();
            var sort  = document.getElementById('sort').value;
            var lista = alla.filter(function(d) {
                return !sok || d.doman.toLowerCase().includes(sok);
            });
            if (sort === 'billigast') lista.sort(function(a, b) { return a.pris - b.pris; });
            else if (sort === 'dyrast') lista.sort(function(a, b) { return b.pris - a.pris; });

            var totalt = lista.length;
            var start  = (sida - 1) * PER_SIDA;
            var sida_items = lista.slice(start, start + PER_SIDA);

            var tbody = document.getElementById('tabell-body');
            if (sida_items.length === 0) {
                tbody.innerHTML = '';
                document.getElementById('tom-msg').style.display = 'block';
            } else {
                document.getElementById('tom-msg').style.display = 'none';
                tbody.innerHTML = sida_items.map(function(d) {
                    var besk = d.beskrivning ? '<span class="besk-cell">' + d.beskrivning.substring(0, 60) + (d.beskrivning.length > 60 ? '…' : '') + '</span>' : '';
                    var badge = '<span style="display:inline-block;font-size:11px;color:#1a8a3a;background:#eaf6ee;border-radius:99px;padding:2px 8px;margin-left:6px;font-weight:500;">✓ Verifierad</span>';
                    return '<tr>' +
                        '<td>' + d.doman + badge + '</td>' +
                        '<td class="pris-cell">' + d.pris.toLocaleString('sv-SE') + ' kr</td>' +
                        '<td>' + besk + '</td>' +
                        '<td><a class="kop-btn" href="/kop-begagnad/' + encodeURIComponent(d.doman) + '">Köp →</a></td>' +
                        '</tr>';
                }).join('');
            }

            var sidor = Math.ceil(totalt / PER_SIDA);
            var pag = document.getElementById('paginering');
            if (sidor <= 1) { pag.innerHTML = ''; return; }
            pag.innerHTML = Array.from({length: sidor}, function(_, i) {
                return '<button class="sid-btn' + (i + 1 === sida ? ' aktiv' : '') + '" onclick="bytSida(' + (i + 1) + ')">' + (i + 1) + '</button>';
            }).join('');
        }

        function bytSida(n) { sida = n; visa(); window.scrollTo(0, 0); }
        visa();
    </script>
</body>
</html>
'''

TACK_BEGAGNAD_HTML = '''<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{% if ok %}{{ doman }} är din{% else %}Betalning mottagen{% endif %} — Namnverket</title>
    <meta name="robots" content="noindex, nofollow">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; max-width: 560px; margin: 120px auto; padding: 0 24px; color: #0a0a0a; text-align: center; }
        .logo { font-size: 11px; letter-spacing: 0.15em; color: #a0a0a0; margin-bottom: 3rem; }
        h1 { font-size: 32px; font-weight: 500; letter-spacing: -0.02em; margin-bottom: 12px; }
        p { font-size: 15px; color: #6b6b6b; margin-bottom: 8px; }
        .doman-namn { font-weight: 500; color: #0a0a0a; }
        a { display: inline-block; margin-top: 24px; font-size: 14px; color: #0a0a0a; text-decoration: none; border-bottom: 0.5px solid rgba(0,0,0,0.2); padding-bottom: 2px; }
        a:hover { border-color: #0a0a0a; }
        .info { font-size: 13px; color: #a0a0a0; margin-top: 20px; }
        @media (max-width: 600px) { body { padding: 0 16px; margin-top: 40px; } h1 { font-size: 26px; } }
    </style>
</head>
<body>
    <header><p class="logo">NAMNVERKET</p></header>
    <main>
    {% if ok %}
    <h1>&#x2714;&#xFE0F; {{ doman }} är din!</h1>
    <p>Betalningen är bekräftad. Överlåtelsen av <span class="doman-namn">{{ doman }}</span> påbörjas nu.</p>
    <p class="info">Du får en bekräftelse till {{ email }} när överlåtelsen är klar. Det tar normalt 1–24 timmar.</p>
    {% else %}
    <h1>Betalning mottagen</h1>
    <p>Vi hanterar överlåtelsen av <span class="doman-namn">{{ doman }}</span> och hör av oss till dig.</p>
    {% if fel %}<p class="info" style="color:#dc2626;">{{ fel }}</p>{% endif %}
    {% endif %}
    <a href="/">Tillbaka till sökningen →</a>
    </main>
</body>
</html>
'''

@app.route('/salj', methods=['GET', 'POST'])
def salj():
    if request.method == 'GET':
        return render_template_string(SALJ_HTML)

    body = request.get_json() or {}
    doman    = _sanera_text(body.get('doman', '').strip().lower(), max_len=253)
    pris_raw = body.get('pris')
    email    = _sanera_text(body.get('email', '').strip(), max_len=200)
    besk     = _sanera_text(body.get('beskrivning', ''), max_len=500)

    if not _valider_doman(doman):
        return jsonify({'ok': False, 'error': 'Ogiltigt domännamn.'})
    try:
        pris = int(pris_raw)
        if pris < 100:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Priset måste vara minst 100 kr.'})
    if not _valider_email(email):
        return jsonify({'ok': False, 'error': 'Ogiltig e-postadress.'})

    verify_code = f"namnverket-verify={secrets.token_hex(16)}"
    print(f'[SÄLJ] doman={doman!r} pris={pris} email={email!r} kod={verify_code}', flush=True)
    try:
        con = sqlite3.connect(DB)
        # Ta bort ev. gammal pending för samma domän så säljaren kan försöka igen
        con.execute("DELETE FROM pending_listings WHERE doman=?", (doman,))
        con.execute(
            "INSERT INTO pending_listings (doman, pris, saljare_email, beskrivning, verify_code) VALUES (?,?,?,?,?)",
            (doman, pris, email, besk, verify_code)
        )
        con.commit()
        con.close()
    except Exception as e:
        print(f'[SÄLJ] DB-fel: {e}', flush=True)
        return jsonify({'ok': False, 'error': 'Kunde inte spara, försök igen.'})

    return jsonify({'ok': True, 'pending': True, 'doman': doman, 'verify_code': verify_code})


@app.route('/verifiera/<path:doman>')
@limiter.limit('10 per minute')
def verifiera(doman):
    doman = doman.strip().lower()
    if not _valider_doman(doman):
        return jsonify({'ok': False, 'error': 'Ogiltig domän'}), 400

    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT pris, saljare_email, beskrivning, verify_code FROM pending_listings WHERE doman=?",
        (doman,)
    ).fetchone()
    con.close()

    if not row:
        return jsonify({'ok': False, 'error': 'Ingen väntande listning hittades för den här domänen.'})

    pris, email, besk, verify_code = row

    if not _kolla_dns_verifiering(doman, verify_code):
        return jsonify({
            'ok': False,
            'pending': True,
            'error': 'TXT-posten hittades inte än. DNS-ändringar kan ta upp till 60 minuter. Försök igen snart.'
        })

    # DNS OK — flytta till domanmarknaden
    try:
        con = sqlite3.connect(DB)
        try:
            con.execute(
                "INSERT INTO domanmarknaden (doman, pris, saljare_email, beskrivning) VALUES (?,?,?,?)",
                (doman, pris, email, besk)
            )
        except sqlite3.IntegrityError:
            # Redan listad (t.ex. dubbel-klick) — uppdatera bara status
            con.execute(
                "UPDATE domanmarknaden SET status='aktiv' WHERE doman=?",
                (doman,)
            )
        con.execute("DELETE FROM pending_listings WHERE doman=?", (doman,))
        con.commit()
        con.close()
    except Exception as e:
        print(f'[VERIFIERA] DB-fel: {e}', flush=True)
        return jsonify({'ok': False, 'error': 'Databasfel, kontakta support.'})

    _logga_email(
        email,
        f'Din domän {doman} är verifierad och listad!',
        f'Grattis! {doman} är nu verifierad och listad till salu för {pris:,} kr på Namnverket.\n'
        f'Vi kontaktar dig så fort en köpare dyker upp.\n\nNamnverket'
    )
    print(f'[VERIFIERA] {doman} verifierad och listad, email={email}', flush=True)
    return jsonify({'ok': True})


@app.route('/marknadsplats')
def marknadsplats():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT doman, pris, beskrivning, listad FROM domanmarknaden WHERE status='aktiv' ORDER BY listad DESC"
    ).fetchall()
    con.close()
    domaner = [{'doman': r[0], 'pris': r[1], 'beskrivning': r[2] or '', 'listad': r[3]} for r in rows]
    return render_template_string(MARKNADSPLATS_HTML, domaner=domaner)


@app.route('/kop-begagnad/<path:doman>')
@limiter.limit('10 per minute')
def kop_begagnad(doman):
    doman = doman.strip().lower()
    if not _valider_doman(doman):
        return 'Ogiltig domän', 400
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT pris, saljare_email FROM domanmarknaden WHERE doman=? AND status='aktiv'",
        (doman,)
    ).fetchone()
    con.close()
    if not row:
        return 'Domänen är inte längre till salu.', 404
    pris, saljare_email = row[0], row[1]
    sid   = get_session_id()
    email = unquote(request.cookies.get('nk_email', '').strip()) or ''
    if email and not _valider_email(email):
        email = ''
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            customer_email=email or None,
            line_items=[{
                'price_data': {
                    'currency': 'sek',
                    'product_data': {'name': f'Domän: {doman} (begagnad)'},
                    'unit_amount': pris * 100,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=request.host_url + 'tack-begagnad?stripe_session={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url + 'marknadsplats',
            metadata={'doman': doman, 'session_id': sid, 'saljare_email': saljare_email, 'typ': 'begagnad'},
        )
    except Exception as e:
        print(f'[KÖP-BEGAGNAD] Stripe-fel: {e}', flush=True)
        return 'Betalning kunde inte skapas, försök igen.', 500
    resp = redirect(session.url, code=303)
    resp.set_cookie('sid', sid, max_age=60 * 60 * 24 * 365, samesite='Lax')
    return resp


@app.route('/tack-begagnad')
def tack_begagnad():
    stripe_session_id = request.args.get('stripe_session', '').strip()
    doman = ''; email = ''; ok = False; fel = ''

    if not stripe_session_id:
        return redirect('/')

    nybetald = markera_betald(stripe_session_id)
    try:
        sess = stripe.checkout.Session.retrieve(stripe_session_id)
        if sess.payment_status == 'paid':
            email = getattr(sess, 'customer_email', None) or ''
            try:
                doman          = sess.metadata['doman']
                saljare_email  = sess.metadata.get('saljare_email', '')
                betalat_pris   = getattr(sess, 'amount_total', 0) // 100
            except (KeyError, AttributeError, TypeError):
                doman = ''; saljare_email = ''; betalat_pris = 0

            print(f'[TACK-BEGAGNAD] doman={doman!r} email={email!r} pris={betalat_pris} nybetald={nybetald}', flush=True)

            if nybetald and doman:
                provision = math.ceil(betalat_pris * 0.10)
                try:
                    con = sqlite3.connect(DB)
                    con.execute(
                        "UPDATE domanmarknaden SET status='såld' WHERE doman=?",
                        (doman,)
                    )
                    con.execute(
                        'INSERT INTO provisioner (doman, forsaljningspris, provision, kopare_email, saljare_email, stripe_session_id) VALUES (?,?,?,?,?,?)',
                        (doman, betalat_pris, provision, email, saljare_email, stripe_session_id)
                    )
                    con.commit()
                    con.close()
                    ok = True
                except Exception as e:
                    print(f'[TACK-BEGAGNAD] DB-fel: {e}', flush=True)
                    fel = 'Databasfel — kontakta support.'

                # Logga email-bekräftelser (stub)
                if email:
                    _logga_email(
                        email,
                        f'Du har köpt domänen {doman}',
                        f'Grattis! Du har köpt {doman} för {betalat_pris:,} kr.\n'
                        f'Överlåtelsen påbörjas nu och du hör av dig inom 24 timmar.\n\nNamnverket'
                    )
                if saljare_email:
                    saljare_netto = betalat_pris - provision
                    _logga_email(
                        saljare_email,
                        f'Din domän {doman} är såld!',
                        f'Grattis — {doman} är såld för {betalat_pris:,} kr!\n'
                        f'Din utbetalning: {saljare_netto:,} kr (efter 10% provision).\n'
                        f'Vi kontaktar dig för att slutföra överlåtelsen.\n\nNamnverket'
                    )
    except Exception as e:
        print(f'[TACK-BEGAGNAD] Stripe-fel: {e}', flush=True)
        fel = str(e)

    resp = make_response(render_template_string(TACK_BEGAGNAD_HTML, ok=ok, doman=doman, email=email, fel=fel))
    if email:
        resp.set_cookie('nk_email', email, max_age=60 * 60 * 24 * 365, samesite='Lax')
    return resp


@app.route('/sitemap.xml')
def sitemap():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://namnverket.se/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>
  <url><loc>https://namnverket.se/generator</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>
  <url><loc>https://namnverket.se/kop-doman</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>
  <url><loc>https://namnverket.se/trender</loc><changefreq>daily</changefreq><priority>0.7</priority></url>
  <url><loc>https://namnverket.se/domanmarknaden</loc><changefreq>daily</changefreq><priority>0.7</priority></url>
  <url><loc>https://namnverket.se/kolla-foretagsnamn</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://namnverket.se/registrera-doman-se</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://namnverket.se/vad-ar-ett-varumarke</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://namnverket.se/marknadsplats</loc><changefreq>daily</changefreq><priority>0.8</priority></url>
  <url><loc>https://namnverket.se/salj</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://namnverket.se/favoriter</loc><changefreq>weekly</changefreq><priority>0.5</priority></url>
</urlset>'''
    return app.response_class(xml, mimetype='application/xml')

@app.route('/robots.txt')
def robots():
    txt = '''User-agent: *
Allow: /
Disallow: /stripe-webhook
Disallow: /admin
Sitemap: https://namnverket.se/sitemap.xml'''
    return app.response_class(txt, mimetype='text/plain')

if __name__ == '__main__':
    app.run(debug=True, port=5001)

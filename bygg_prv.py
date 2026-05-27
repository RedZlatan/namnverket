"""
Laddar hem PRV:s varumärkesdatabas via FTP och bygger tabellen varumärken i bolag.db.
FTP: ftp://opendata.prv.se  |  login: OpenDataSource / opendata
"""
from ftplib import FTP, error_temp
import sqlite3, zipfile, io, re, time

FTP_HOST = 'opendata.prv.se'
FTP_USER = 'OpenDataSource'
FTP_PASS = 'opendata'
FTP_DIR  = '/TrademarkExport/NewExport/trademark/data/full'
DB_PATH  = 'bolag.db'

TAG_TM     = re.compile(r'<TradeMark[^>]*>(.*?)</TradeMark>', re.DOTALL)
TAG_NAME   = re.compile(r'<MarkVerbalElementText>(.*?)</MarkVerbalElementText>', re.DOTALL)
TAG_STATUS = re.compile(r'<MarkCurrentStatusCode>(.*?)</MarkCurrentStatusCode>', re.DOTALL)
TAG_CLASS  = re.compile(r'<ClassNumber>(.*?)</ClassNumber>', re.DOTALL)
TAG_KIND   = re.compile(r'<KindMark>(.*?)</KindMark>', re.DOTALL)

def connect_ftp():
    ftp = FTP(timeout=60)
    ftp.connect(FTP_HOST)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    ftp.cwd(FTP_DIR)
    return ftp

print('Ansluter till FTP...')
ftp = connect_ftp()
files = sorted(f for f in ftp.nlst() if f.endswith('.zip'))
print(f'Hittade {len(files)} filer.')

print('Skapar tabell varumärken i databasen...')
con = sqlite3.connect(DB_PATH)
cur = con.cursor()
cur.execute('DROP TABLE IF EXISTS varumärken')
cur.execute('''CREATE TABLE varumärken (
    namn              TEXT,
    status            TEXT,
    klass             TEXT,
    organisationsform TEXT
)''')
con.commit()

batch = []
total = 0
errors = 0

for i, filename in enumerate(files, 1):
    xml = ''
    for attempt in range(3):
        try:
            buf = io.BytesIO()
            ftp.retrbinary(f'RETR {filename}', buf.write)
            buf.seek(0)
            zf = zipfile.ZipFile(buf)
            xml = zf.read(zf.namelist()[0]).decode('utf-8', errors='replace')
            break
        except Exception:
            if attempt < 2:
                time.sleep(2)
                try:
                    ftp.quit()
                except Exception:
                    pass
                ftp = connect_ftp()
            else:
                errors += 1

    for tm in TAG_TM.findall(xml):
        names = TAG_NAME.findall(tm)
        if not names:
            continue
        status = (TAG_STATUS.findall(tm) or [''])[0].strip()
        klass  = ','.join(c.strip() for c in TAG_CLASS.findall(tm))
        kind   = (TAG_KIND.findall(tm) or [''])[0].strip()
        for name in names:
            batch.append((name.strip(), status, klass, kind))

    if len(batch) >= 20000:
        con.executemany('INSERT INTO varumärken VALUES (?,?,?,?)', batch)
        con.commit()
        total += len(batch)
        batch = []

    if i % 50 == 0 or i == len(files):
        print(f'  {i}/{len(files)} filer klara, {total + len(batch):,} poster hittills...')

if batch:
    con.executemany('INSERT INTO varumärken VALUES (?,?,?,?)', batch)
    con.commit()
    total += len(batch)

cur.execute('CREATE INDEX idx_vm_namn ON varumärken(namn)')
con.commit()

count = cur.execute('SELECT COUNT(*) FROM varumärken').fetchone()[0]
print(f'Klart! {count:,} varumärkesposter. ({errors} felfiler)')
con.close()
try:
    ftp.quit()
except Exception:
    pass

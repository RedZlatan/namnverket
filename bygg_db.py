import sqlite3, csv

print('Rensar och bygger om databas...')
con = sqlite3.connect('../bolag.db')
cur = con.cursor()

cur.execute('DROP TABLE IF EXISTS bolag')
cur.execute('''CREATE TABLE bolag (
    namn TEXT,
    orgnr TEXT,
    bolagsform TEXT,
    reg_datum TEXT,
    status TEXT,
    beskrivning TEXT
)''')

def extrahera_namn(falt):
    return falt.split('$FORETAGSNAMN')[0].strip()

def extrahera_orgnr(falt):
    return falt.split('$ORGNR')[0].strip()

with open('bolagsverket_bulkfil.txt', encoding='utf-8', errors='ignore') as f:
    reader = csv.reader(f, delimiter=';', quotechar='"')
    batch = []
    for i, row in enumerate(reader):
        if len(row) >= 5:
            namn = extrahera_namn(row[3])
            orgnr = extrahera_orgnr(row[0])
            bolagsform = row[4]
            reg_datum = row[8] if len(row) > 8 else ''
            status = row[4]
            beskrivning = row[9].strip() if len(row) > 9 else ''
            batch.append((namn, orgnr, bolagsform, reg_datum, status, beskrivning))
        if i % 50000 == 0 and i > 0:
            print(f'  {i:,} rader...')
            con.executemany('INSERT INTO bolag VALUES (?,?,?,?,?,?)', batch)
            con.commit()
            batch = []
    if batch:
        con.executemany('INSERT INTO bolag VALUES (?,?,?,?,?,?)', batch)
        con.commit()

cur.execute('CREATE INDEX idx_namn ON bolag(namn)')
count = cur.execute('SELECT COUNT(*) FROM bolag').fetchone()[0]
print(f'Klart! {count:,} bolag i databasen.')
con.close()

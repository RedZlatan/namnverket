---
titel: Därför behöver du inte betala för SSL
datum: 2026-05-29
beskrivning: Openprovider och andra domänregistratorer försöker sälja dig SSL-certifikat. Men det finns ett gratis alternativ som är lika bra.
---

När du registrerar en domän hos Openprovider, Loopia eller någon annan registrator möts du förr eller senare av ett erbjudande: *"Skydda din sajt med SSL – från 299 kr/år."* Det låter viktigt. Det är det inte.

## Vad är SSL och varför behöver du det?

SSL (numera TLS) är det som gör att din webbadress börjar med `https://` istället för `http://`. Det krypterar trafiken mellan din server och besökarens webbläsare. Utan det varnar Chrome dina besökare för att sajten är "osäker", och Google rankar dig lägre i sökresultaten.

Du behöver alltså SSL. Men du behöver inte *köpa* det.

## Let's Encrypt – gratis, automatiskt, lika säkert

[Let's Encrypt](https://letsencrypt.org) är en gratis certifikatutfärdare som drivs av en ideell organisation. Certifikaten är tekniskt identiska med det Openprovider säljer för 299 kr. Skillnaden är att de är gratis och förnyas automatiskt var 90:e dag.

Bakom Let's Encrypt står Mozilla, EFF och Cisco. Det är med andra ord inte en hobbyist i ett garage – det är internet-infrastruktur i toppklass.

## Installera med Certbot på Ubuntu

Det tar ungefär tre minuter:

```bash
# Installera Certbot
sudo apt install certbot python3-certbot-nginx

# Hämta och installera certifikat
sudo certbot --nginx -d dittdomännamn.se

# Klart. Certbot sätter upp automatisk förnyelse.
```

Certbot konfigurerar även din nginx automatiskt och lägger in ett cron-jobb som förnyar certifikatet innan det löper ut. Du behöver aldrig tänka på det igen.

## Kort sagt

Betala inte för SSL. Registratorerna tjänar pengar på att du inte vet bättre. Let's Encrypt finns, det är gratis, och det fungerar utmärkt – precis som det SSL du annars hade betalat för.

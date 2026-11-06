# Volvo2mqtt 2026.11.2

Oma Docker-versio, pohjana Dielee/volvo2mqtt commit 25045e90e1899aa7b994bf2c7d2e933b041b4834.

## Tila

Keycloak-kirjautuminen, tokenin uusiminen ja rotaatio on testattu oikealla tilillä. Sensorien ja lisäpainikkeiden MQTT-discovery on testattu. Aiemman koekäytön jälkeen komentojen jonotus ja hakujen rytmitys uusittiin; nämä viimeisimmät muutokset on testattu automaattisesti, mutta niiden autolla tehtävä hyväksymiskoe on vielä kesken.

Tämä julkaisu tukee itsenäistä Docker-asennusta. Mukana oleva upstreamin HA-lisäosarakenne ei ole tässä julkaisussa toimivaksi sovitettu tai testattu. Vanha OTP-anturi jää vanhaan haaraan.

## Muutokset 2026.11.2

- Lukitus ja avaus kirjataan onnistuneiksi vasta, kun tilahaku vahvistaa ne. Vahvistamaton komento voidaan toistaa heti.
- Esilämmityksen tavoite viedään jonoon atomisesti. Vaimennetun toiston takaisinkutsu ajetaan jonon lukon ulkopuolella.
- Päivityspainike, päivitysvälin muutos ja ajastuksen asetus eivät enää aja hakuja MQTT-säikeessä.
- Volvon hylkäämää refresh tokenia ei lähetetä uudelleen ennen uutta kirjautumista tai uudelleenkäynnistystä.
- Ajotila nollaa esilämmitysoletuksen vain, kun oletus on päällä, ja nollaus kirjataan lokiin.
- Komentotopicit tilataan kerran, ja discovery-config julkaistaan uudelleen vain sen muuttuessa.
- API-avaimen vaihto ohittaa välimuistin. Latausteho lukee yksikön myös `data`-kääreestä.
- Web-kirjautuminen: pyyntöjen aikakatkaisu ja viive virheellisen salasanan jälkeen. Lokissa ei enää näy VIN-numeroa.

## Mukana

- Uusi Volvo Keycloak Authorization Code + PKCE, oma Client ID ja Client Secret.
- Selainkirjautuminen ja suojattu callback. Tokenit pysyvässä data-hakemistossa.
- Refresh-tokenien rotaatio, yhteinen token-käsittelijä MQTT- ja selainpalvelulle.
- Energy v2 ja viisi uutta latausanturia.
- Auton tuen mukaan valot, äänimerkki, niiden yhdistelmä ja reduced guard -lukitus.
- Esilämmityksen toggle säilyy paikallisena oletuksena, yksi 30 minuutin ajastin.
- Autokohtainen komentojono: 1 s rauhoittuminen, viimeinen tavoite voittaa; komennot sarjassa, 3 s väli, saman onnistuneen tavoitteen toisto vaimennetaan 30 sekunniksi.
- Normaalissa haussa havaittu ajotila päättää esilämmitysoletuksen. Ei viiden sekunnin taustakyselyä tai oven avaamiseen perustuvaa oletusta.
- Lukituksen varmistus enintään kolmella ovitilahaulla, 10 s välein.
- Yhteinen osoitekohtainen välimuisti ja päällekkäisten kokonaispäivitysten esto.
- Ovet/ikkunat 60 s; ajotila 120 s; Energy 120 s ladattaessa, muuten 300 s; sijainti/matkamittari/tilastot 900 s; diagnostiikka/renkaat/varoitukset 1800 s; perustiedot 86400 s.
- Ajoitussilmukka tarkistaa välimuistin 30 s välein. updateInterval on muiden hakujen oletusväli, ei enää kaikkien ryhmien yhteinen päivitysväli.
- Loki laskee ajoneuvo-API:n HTTP-yritykset menetelmittäin. Prosessikohtainen laskuri nollautuu UTC-päivän vaihtuessa ja uudelleenkäynnistyksessä.
- Nullsafe-käsittely sekä pitkä backend-ilmoitus message-attribuuttina, tila enintään 255 merkkiä.

## Käyttöönotto

1. Luo ja julkaise uusi Volvo-sovellus, salli tarvittavat scopes-oikeudet ja rekisteröi oman palvelusi HTTPS-callback.
2. Kopioi settings.keycloak.example.json nimelle settings.json. Täytä tunnukset vain paikallisesti. Älä julkaise tiedostoa.
3. Sovita compose.keycloak.yaml-tiedoston IP-sidonta ja web_login.py-tiedoston ORIGIN omaan verkkotunnukseesi. Tämä fork on alun perin sovitettu tekijän kotiympäristöön; asetuspohja ei ole yleiskäyttöinen sellaisenaan.
4. Luo data-hakemisto ja web-password-tiedosto, rajaa oikeudet. web-password on vähintään 24 merkin satunnainen salasana /login-sivun admin-käyttäjälle.
5. Rakenna kuva, suorita tarvittaessa setup_keycloak.py paikallisessa terminaalissa. Salaisuussyötteet ovat piilotettuja.
6. Ensimmäistä kirjautumista varten compose.web.yaml voi ajaa erillistä web-palvelua. Kun pääpalvelu käynnistetään, pysäytä erillinen web-palvelu. Älä aja kahta tokenin omistajaa tai MQTT-versiota yhtä aikaa.
7. Pääpalvelu sisältää saman /login-, /oauth/callback-, /terms- ja /health-palvelun. Ohjaa HTTPS-tunneli sen porttiin 8080. Älä paljasta asetustiedostoja tai data-hakemistoa verkkopalvelussa.

Tällä testatulla Volvo-sovelluksella offline_access hylättiin, mutta refresh token saatiin ja uusiminen onnistui ilman sitä. Asetus oauth.request_offline_access=false ottaa tämän käyttöön; oletus on true. Tämä on palvelimen sovelluskohtainen havainto, ei yleinen lupaus tokenien kestosta.

compose.evening.yaml valitsee erillisen paikallisen keycloak-evening-kuvan, joka pitää rakentaa tällä nimellä ennen käyttöä. Se ei ole julkaistu konttirekisteriin.

## Uudelleenkirjautuminen

Avaa /login, käyttäjä admin ja web-password-tiedoston salasana. Kirjaudu Volvon omalla sivulla ja tee sen vaatimat vahvistukset. Callback tallentaa tokenit, ja pääpalvelu jatkaa samalla token-käsittelijällä. CLI-kirjautumista ei ajeta pääpalvelun rinnalla.

## Testaus ja palautus

49 automaattista testiä läpäisty myös Dockerissa verkkoyhteys estettynä. Testit ajetaan komennolla python -m unittest discover -s tests -v. Mock-testit eivät korvaa auton käyttäytymisen hyväksymiskoetta.

Säilytä vanha kontti ja ota LXC-varmuuskopio. Pysäytä vanha ennen uuden MQTT-palvelun käynnistämistä. Palautus: pysäytä uusi, käynnistä vanha. Uusien entiteettien discovery-tiedot jäävät brokeriin, kunnes ne siivotaan erikseen. Älä palauta vanhentunutta refresh tokenia aktiiviseksi kiertävän tokenin tilalle.

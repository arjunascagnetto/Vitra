# Google Auth Plan

## Obiettivo

Aggiungere accesso autenticato tramite Google OAuth, consentendo l'accesso solo a una lista precisa di utenti autorizzati.

L'utente `arjuna.scagnetto@gmail.com` deve essere amministratore.

## Approccio

Usare Google OAuth / OpenID Connect. Non introdurre password locali, registrazione libera o gestione credenziali custom.

Flusso previsto:

1. L'utente apre la web app.
2. Se non è autenticato, viene mandato alla pagina di login Google.
3. Google restituisce l'identità verificata dell'utente.
4. Il backend legge email, nome e avatar.
5. Il backend controlla l'email contro una allowlist esplicita.
6. Se l'email non è autorizzata, risponde con `403`.
7. Se l'email è autorizzata, crea una sessione applicativa.
8. Se l'email è `arjuna.scagnetto@gmail.com`, assegna ruolo `admin`.

## Configurazione

Le configurazioni sensibili devono stare in `.env`, non nel codice.

Variabili previste:

```bash
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
SESSION_SECRET=...
ALLOWED_USERS=arjuna.scagnetto@gmail.com,altro@example.com
ADMIN_USERS=arjuna.scagnetto@gmail.com
```

`ALLOWED_USERS` rimane la fonte di verità per l'accesso.

`ADMIN_USERS` definisce gli utenti amministratori.

## Google Cloud Console

Serve creare un OAuth Client in Google Cloud Console.

Redirect URI locale:

```text
http://127.0.0.1:8000/auth/google/callback
```

In produzione o su altro host, aggiungere il redirect URI corrispondente.

## Libreria

Usare una libreria OAuth affidabile per FastAPI/Starlette.

Scelta proposta:

```text
Authlib
```

Motivo:

- supporta OAuth2/OpenID Connect
- si integra con Starlette/FastAPI
- evita implementazioni manuali del protocollo OAuth

## Sessione

Usare session cookie firmato tramite middleware Starlette.

Proprietà desiderate:

- `httponly`
- `samesite=lax`
- `secure=false` in locale
- `secure=true` quando servito dietro HTTPS

La sessione dovrebbe contenere solo dati minimi:

- email
- nome
- avatar
- ruolo

Non salvare token Google se non servono.

## Route Pubbliche

Route da lasciare pubbliche:

- `/login`
- `/auth/google/callback`
- `/logout`
- eventuali asset statici necessari alla pagina login

## Route Protette

Route da proteggere:

- `/`
- `/api/videos`
- `/api/videos/{id}`
- `/api/videos/{id}/audio`
- export TXT/PDF/JSON
- processing nuovi video
- qualsiasi endpoint futuro di gestione archivio

## Utenti e Ruoli

Creare helper applicativi concettuali:

- utente corrente
- richiesta utente autenticato
- richiesta admin

Ruoli iniziali:

- `user`
- `admin`

`arjuna.scagnetto@gmail.com` deve essere `admin`.

## Database

Non è strettamente necessario salvare utenti nel DB per autorizzare l'accesso, perché la fonte di verità è `.env`.

È però utile aggiungere una tabella `users` per audit locale:

- email
- name
- picture
- role
- created_at
- last_login_at

Questa tabella non deve autorizzare utenti non presenti in `ALLOWED_USERS`.

## UI

Modifiche previste:

- schermata login se non autenticato
- pulsante "Accedi con Google"
- header con email utente loggato
- badge `admin` per amministratori
- pulsante logout

La UI non deve mostrare funzionalità protette se l'utente non è autenticato.

## Cose da Evitare

Non fare:

- password locali
- registrazione libera
- autorizzazione basata solo sul dominio email
- accesso per qualunque account Google
- salvataggio di token Google non necessari
- hardcoding di segreti o utenti nel codice

## Ordine di Implementazione

1. Aggiungere dipendenza OAuth.
2. Aggiornare `.env.example`.
3. Aggiungere middleware sessione.
4. Aggiungere route login, callback, logout e user info.
5. Aggiungere controllo allowlist.
6. Proteggere API e pagine.
7. Aggiungere UI login/logout.
8. Aggiungere tabella utenti/audit se utile.
9. Verificare login consentito per `arjuna.scagnetto@gmail.com`.
10. Verificare blocco per utente non autorizzato.
11. Aggiornare documentazione.
12. Fare commit separato.

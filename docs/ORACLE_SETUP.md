# Creazione VPS Oracle Cloud Always Free

## Prompt per l'agente Chrome

Copia tutto il blocco sotto e incollalo all'agente che opera in Chrome.

```text
Obiettivo: creare UNA istanza di calcolo Oracle Cloud "Always Free" per ospitare un backend Docker.
Costo consentito: ZERO. Regole vincolanti:
- NON cliccare mai "Upgrade to Pay As You Go", "Upgrade account" o simili. NON acquistare nulla.
- Usa solo risorse marcate "Always Free-eligible". Se un'opzione non è Always Free, fermati e chiedimi.
- Se Oracle chiede dati di pagamento OLTRE la verifica iniziale dell'account, fermati e chiedimi.
- Non creare più di un'istanza, né Load Balancer, né IP pubblici riservati a pagamento, né volumi aggiuntivi.
- Se una pagina chiede conferme ambigue o costi, fermati e chiedimi.

0. Se non ho ancora un account: vai su https://signup.cloud.oracle.com, crea l'account Free Tier.
   Home Region: "Italy Northwest (Milan)" se disponibile, altrimenti "Germany Central (Frankfurt)".
   ATTENZIONE: la home region non si può cambiare. La verifica carta prevede solo una pre-autorizzazione temporanea.
   Fermati e lascia a me l'inserimento dei dati della carta e gli eventuali codici SMS/email.

1. Console: https://cloud.oracle.com → menu ☰ → Compute → Instances → "Create instance".

2. Name: nowcast-vps
   Compartment: quello root (default).

3. Placement: lascia l'Availability Domain predefinito (se più avanti esce "Out of capacity", torna qui e prova AD-2, poi AD-3).

4. Image and shape → "Edit":
   - Image: "Change image" → Canonical Ubuntu → versione 24.04 → scegli la build per aarch64 (ARM) → Select.
   - Shape: "Change shape" → Virtual machine → Ampere → VM.Standard.A1.Flex.
     Imposta OCPU = 2 e Memory = 12 GB. Verifica che compaia l'etichetta "Always Free-eligible".
     Se il limite consentito è più basso, usa il massimo consentito che resti "Always Free-eligible".

5. Networking:
   - "Create new virtual cloud network" (nome: nowcast-vcn) e "Create new public subnet" (default).
   - "Automatically assign public IPv4 address": ATTIVO.

6. Add SSH keys:
   - Seleziona "Generate a key pair for me".
   - Clicca "Save private key" e "Save public key" (scaricano due file .key/.pub). Annota il nome del file privato.

7. Boot volume:
   - "Specify a custom boot volume size": 100 GB (Always Free include fino a 200 GB totali).
   - Lascia "in-transit encryption" com'è.

8. "Show advanced options" → tab "Management" → "Initialization script" → "Paste cloud-init script"
   e incolla esattamente questo testo:

#cloud-config
package_update: true
packages: [rsync, iptables-persistent, ca-certificates, curl]
runcmd:
  - iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp -m state --state NEW --dport 80 -j ACCEPT
  - iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp -m state --state NEW --dport 443 -j ACCEPT
  - netfilter-persistent save
  - '[ -f /swapfile ] || (fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo "/swapfile none swap sw 0 0" >> /etc/fstab)'
  - curl -fsSL https://get.docker.com | sh
  - systemctl enable --now docker
  - usermod -aG docker ubuntu
  - mkdir -p /opt/nowcast

9. Controlla il riepilogo "Estimated cost" / etichette: deve risultare tutto Always Free (costo 0).
   Poi "Create".
   - Se compare "Out of host capacity": torna al punto 3, cambia Availability Domain e riprova.
     Se falliscono tutti gli AD, riprova ogni 10-15 minuti al massimo 6 volte, poi fermati e avvisami.

10. Quando lo stato è "RUNNING", annota il "Public IPv4 address" dalla pagina dell'istanza.

11. Apri le porte web: nella pagina dell'istanza → sezione Primary VNIC → clicca la Subnet →
    tab "Security" (o "Security Lists") → "Default Security List for nowcast-vcn" → "Add Ingress Rules":
    - Regola 1: Source CIDR 0.0.0.0/0, IP Protocol TCP, Destination Port Range 80, descrizione "HTTP".
    - Regola 2: Source CIDR 0.0.0.0/0, IP Protocol TCP, Destination Port Range 443, descrizione "HTTPS".
    Salva. Non toccare la regola esistente della porta 22.

12. Report finale da darmi: Public IPv4, region, availability domain, shape (OCPU/GB), nome del file della chiave privata scaricata.
```

## Dopo l'agente (manuale, ~3 minuti)

GitHub → `simogerardini/nowcasting` → Settings → Secrets and variables → Actions:

| Tipo | Nome | Valore |
|---|---|---|
| Variables | `VPS_HOST` | IPv4 pubblico dell'istanza |
| Secrets | `VPS_SSH_KEY` | contenuto completo del file chiave privata scaricato (`-----BEGIN ... PRIVATE KEY-----` … `END`) |
| Secrets | `VPS_USER` | `ubuntu` |
| Secrets | `CLOUDFLARE_API_TOKEN` | token con *Zone → DNS → Edit* su `noctalix.com` (dash.cloudflare.com → My Profile → API Tokens → template "Edit zone DNS") |

Poi Actions → "Deploy backend (VPS)" → Run workflow (<1 min di Actions). La build Docker parte in background sul VPS (prima volta 10–15 min); poi `https://api-nowcast.noctalix.com/api/v1/health` risponde.

## Note anti-costi Oracle
- Non fare upgrade a Pay As You Go.
- Istanza ferma per inattività (CPU p95 < 20% per 7 giorni): viene *fermata*, non cancellata → Console → Instances → Start.
- Dopo 30 giorni di trial i crediti scadono: le risorse Always Free restano, nulla viene addebitato.

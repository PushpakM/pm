# Weighbridge software – Shri Balaji Associates

Weighing tickets, contractor billing at ₹/MT, Tally export, SMS / WhatsApp / email tickets,
and reports with Excel export. It runs on the weighbridge PC and opens in the browser.
It keeps working with no internet; messages wait in an outbox and go out when the line is back.

| Area | What it does |
|---|---|
| Weighing | Reads the indicator on the COM port. Captures only when the weight is stable. First and second weighment, or one pass with a stored tare. Prints a ticket with a customer and an office copy. |
| Fraud controls | Nobody can type a weight (only a supervisor, with a reason, and the ticket is flagged). Weights of a closed ticket can't be changed, even in the database. Flags for an unusual tare, hand-entered weights, long gaps between weighments and after-the-fact corrections. |
| Billing | Per party, per date range: net MT × rate, GST split into CGST+SGST or IGST. Customers get a tax invoice; contractors (e.g. Four Square at ₹93/MT) a work statement. A ticket can be on one bill only. |
| Tally | Download a voucher XML (Sales for customers, Purchase for contractors), or push it straight into Tally Prime. |
| Messages | Ticket image on WhatsApp, SMS text, email with the image attached. Daily summary email to the owner. |
| Reports | By party, material, vehicle or day, with Excel download. |
| Security | Personal logins with roles, two-step login (authenticator app), lockout after wrong passwords, CSRF protection, strict browser security headers, hash-chained audit log that detects tampering, daily backups. |

## 1. Install on the weighbridge PC (Windows 10/11)

1. Install **Python 3.12** from python.org. Tick "Add python.exe to PATH".
2. Copy this folder to the PC, e.g. `C:\Weighbridge`.
3. Double-click **`start.bat`**. The first run installs everything and creates `config.toml`.
4. Open `config.toml` in Notepad and set at least `[company]`.
5. Open a Command Prompt in the folder and create the owner account:
   ```
   .venv\Scripts\python -m weighbridge init
   ```
6. Double-click `start.bat` again, then open **http://127.0.0.1:8080** in Chrome or Edge.
   The first login asks the owner to set up two-step login with Google Authenticator or Microsoft Authenticator.

Until you set up the indicator (step 2) the app runs on a **simulator** that pretends trucks
drive on and off. Use it to train operators; a "SIMULATOR" label shows on the weight display.

## 2. Connect your weight indicator

The software needs to know how your indicator talks. You don't need to know the make.

1. Connect the indicator's RS-232 port to the PC (a USB-to-serial adapter is fine).
   In the indicator's settings, set the serial output to **continuous** if it has that option.
2. Find the COM port number in Windows Device Manager → Ports (COM & LPT).
3. Run the sniffer with a truck (or anything heavy) on the platform:
   ```
   .venv\Scripts\python -m weighbridge sniff --port COM3
   ```
   It tries the common baud rates and prints what arrives, e.g.
   `raw: b'\x02+0012340\x03...'  weights found: [12340, ...]  → Put baudrate = 9600 in config.toml`.
4. In `config.toml` → `[indicator]` set `source = "serial"`, `port`, `baudrate`.
   The default `pattern` works for most indicators. If the weights found don't match the display,
   send me the raw line and I'll give you the exact pattern.
   If the indicator sends tonnes (e.g. `42.56`), set `multiplier = 1000`.
5. Set `capacity_kg` and `stable_tolerance_kg` (the scale interval, usually 10 or 20 kg)
   to match the Legal Metrology stamping certificate.
6. Restart. The System page shows the raw text and the reading.

If the indicator has an Ethernet port instead, use `source = "tcp"` with its IP address and port.

## 3. Set up the masters

- **Parties**: customers and contractors, each with a rate per MT, GSTIN, mobile, WhatsApp and email.
- **Materials**: e.g. Boulder, Metal 20 mm, Dust.
- **Vehicles** are added automatically at first weighing. Add the driver's mobile and the owner's WhatsApp so tickets reach them.
- **Users** (owner only): one account per person. Roles:

| Role | Can |
|---|---|
| Operator | Weigh, print, send tickets. Can't edit or cancel anything. |
| Supervisor | Everything an operator can, plus hand-entered weight (flagged), correct party or material, cancel tickets, edit vehicles and materials. |
| Accounts | Parties and rates, bills, Tally, reports. Can't weigh. |
| Owner / Admin | Everything, including users, cancelling bills, backups and the audit log. Two-step login is compulsory. |
| Auditor | Read-only, including the audit log. |

## 4. Daily use

1. Type the vehicle number. The screen says whether this is a first or a second weighment,
   and offers "use stored tare" when the vehicle has a recent one.
2. Pick the party and material. When the weight shows **Stable** (green), press **Capture weight**.
3. After the second weighment the ticket opens. Print it, and messages go out automatically.
4. Once a week, weigh each empty truck with **Record tare only** to refresh its stored tare.

## 5. WhatsApp, SMS, email, Tally

All settings are in `config.toml`. Restart the app after changing them.

- **Email**: for Gmail use an *app password* (Google Account → Security → App passwords), never your normal password.
- **SMS**: any DLT-registered Indian gateway (MSG91, Fast2SMS, Textlocal, etc.). Put its URL with the `{to}`, `{message}`…
  placeholders, and make `template` match your DLT-approved template word for word.
- **WhatsApp**: needs the WhatsApp Business Cloud API (Meta Business account, phone number ID and a permanent access token).
  Businesses can only message people outside a 24-hour chat window through an approved template, so create a template
  called `weighbridge_ticket` with an **image header** and 3 body variables: ticket number, vehicle, net MT.
- **Tally Prime**: in Tally, F1 → Settings → Connectivity → set *TallyPrime acts as* **Both**, port 9000.
  Create the ledgers named in `[tally]` and one ledger per party (or set "Tally ledger name" on the party).
  Then set `tally.enabled = true` to get the **Send to Tally** button. Otherwise use **Tally XML** and import it in Tally
  (Import → Transactions).

## 6. Backups and security checklist

- A backup is written every day to `data\backups` and the last 30 are kept.
  **Set `backup.extra_dir`** to a USB drive or a Google Drive / OneDrive folder so a copy leaves the PC.
- Keep `server.host = "127.0.0.1"` so only this PC can open the app. To use it from the office PC,
  set it to the PC's LAN address only behind the site firewall, and ask me to add HTTPS first.
- Turn on **BitLocker** for the PC's disk (Windows Pro) so a stolen PC doesn't expose data.
- `config.toml` holds API keys and passwords: right-click → Properties → Security, and allow only the Windows account that runs the app.
- Give the Windows login a password, turn off auto-login, and don't leave AnyDesk or TeamViewer running unattended.
- The owner's daily summary email includes the **audit chain code**. Keep those emails. If someone edits the database
  directly, the Audit page shows "Chain broken" and the codes stop matching.
- To check from the command line: `.venv\Scripts\python -m weighbridge verify`.

### Start automatically with Windows

Task Scheduler → Create Task → *Run whether user is logged on or not* → Trigger: **At startup** →
Action: start `C:\Weighbridge\.venv\Scripts\python.exe` with arguments `-m weighbridge run`, start in `C:\Weighbridge`.

## For developers

```
pip install -r requirements-dev.txt
python -m pytest            # 42 tests: indicator parsing, weighing, billing, Tally, security, web flow
python -m weighbridge run   # uses the simulator by default
```

Code map: `weighbridge/indicator.py` (serial/TCP reader, stability), `services/weighing.py` (tickets),
`services/billing.py`, `services/tally.py`, `services/notify.py` (outbox + senders),
`services/reports.py` (Excel), `audit.py` (hash chain), `security.py` (passwords, TOTP), `web/app.py` (routes).
The in-house build plan is in `docs/weighbridge-inhouse-plan.html`; the operating plan (flows, hardware, cloud vs in-house, integration requirements) is in `docs/weighbridge-software-plan.html`.

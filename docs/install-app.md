# Install CronRack as an app on your phone

CronRack's status page can be installed on your phone's home screen. It then opens full
screen, like a native app: no address bar, no browser tabs, its own icon and its own entry
in the app switcher. It is still the same page from your own server, so there is nothing
to download from an app store and nothing to update separately.

Under the hood this is a *Progressive Web App* (PWA): the server sends a small
description of the app (`/manifest.webmanifest`) and a *service worker* (`/sw.js`), a
small script the phone keeps in the background.

## What the app does offline

Nothing pretends to be live. CronRack never stores your status, hosts or any page on the
phone, because an old status shown as if it were current is worse than none.

When the phone can't reach your server (no signal, the VPN is off, or CronRack itself is
down), the app shows one calm screen instead of a browser error:

> Can't reach your CronRack server.
> Check your connection or VPN, then try again.

**Try again** asks the server again. As soon as it answers, you get the real, live page.

## Requirement: HTTPS with a certificate the phone trusts

Phones only install the full-screen app from an **HTTPS** address whose certificate they
trust. Over plain `http://<ip>`, or with a certificate warning you clicked through,
"Add to Home Screen" only makes a bookmark that opens in the browser, and the offline
screen doesn't work.

Any one of these works:

- **Your own domain** through Caddy (or Traefik) with a Let's Encrypt certificate, for
  example `https://cronrack.example.com`.
  See [DEPLOY.md, 1.3](DEPLOY.md#13-pick-the-name-youll-use).
- **Tailscale HTTPS**: turn on HTTPS certificates in the Tailscale admin console and use
  the server's `*.ts.net` name (`https://<machine>.<tailnet>.ts.net`).
- **Caddy's own certificate on your LAN** (the default): works once the phone trusts
  Caddy's root certificate. See
  [DEPLOY.md, 1.7](DEPLOY.md#17-the-root-certificate-trusting-your-server).

Use the same address you normally open CronRack with. The installed app always opens
that address.

## Scan a QR code instead of typing the address

You don't have to type the address on your phone. CronRack can show a QR code that opens
its install page (`/install`), which shows the install button for your phone.

- **On a computer:** open **Settings, then Phone app**. Scan the code with the phone
  camera. (The code only shows when you opened CronRack over HTTPS with its real name,
  not `localhost`: a phone can't reach "localhost".)
- **In a terminal:**

  ```bash
  docker compose exec app python manage.py phone_qr
  ```

  On Kubernetes, run the same `python manage.py phone_qr` inside the web pod (for
  example with `kubectl exec`; the pod and container names depend on your setup, see
  [kubernetes.md](kubernetes.md)).
- **On the very first start,** before you have created an account, the same code is
  printed under the setup code in `docker compose logs app`.

What the install page shows: on **Android (Chrome)** an **Install CronRack** button; on
**iPhone and iPad** the two steps (**Share**, then **Add to Home Screen**) with small
icons; in a browser that offers neither, a line pointing at the browser menu. If the app
is already installed it says so and links to the dashboard. Over plain `http://` it only
explains that HTTPS is needed (see the requirement below).

**Which address is in the code.** The terminal and the first-start log use
`CRONRACK_PUBLIC_URL` from your `.env`, for example
`CRONRACK_PUBLIC_URL=https://cronrack.example.com` (add `:port` if it is not 443, and no
path). If it is not set, `https://<CRONRACK_DOMAIN>` is used. It must be `https`. If it
is missing or invalid, `phone_qr` prints one line saying so and no code. The Settings
page uses the address you opened it with.

**`ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`.** `CRONRACK_PUBLIC_URL` is only used to
print the code. It does **not** make the server answer to that name. If it is different
from `CRONRACK_DOMAIN`, also add the name to `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`
yourself; `phone_qr` warns you when it is missing.

Tips:

- If the code looks scrambled in the terminal, widen the window: it wraps when the
  line is too short.
- The terminal code is drawn for a **dark** terminal. On a light background it looks
  inverted and many cameras won't read it: use a dark terminal or the Settings page.
- The code only ever contains the address ending in `/install`. Never a login, a token
  or the setup code.

## Android (Chrome)

1. Open your CronRack address in **Chrome** and log in.
2. Tap the **⋮** menu, then **Install app** (on some versions: **Add to Home screen**,
   then **Install**).
3. Confirm. The CronRack icon appears on your home screen and in the app drawer.

If you only see **Add to Home screen** with a **Create shortcut** option, Chrome doesn't
consider the address secure: check the HTTPS requirement above.

## iPhone and iPad (Safari)

1. Open your CronRack address in **Safari** (on iOS 16.4+ other browsers work too, but
   Safari is the safe choice) and log in.
2. Tap the **Share** button (the square with an arrow pointing up).
3. Scroll down and tap **Add to Home Screen**, then **Add**.

The app opens full screen. Its content stays clear of the notch, the Dynamic Island and
the home bar.

## Updating and removing

- **Updates** arrive by themselves: the app always loads the current page from your
  server, and the phone checks for a new service worker on every start.
- **Remove**: long-press the icon and choose **Uninstall** (Android) or **Remove App** >
  **Delete from Home Screen** (iPhone). This also removes the offline screen's few stored
  files (the page, its stylesheet, script, font and icons).

## Troubleshooting

- **The app opens with an address bar.** It was added as a bookmark, not installed:
  the address wasn't HTTPS with a trusted certificate when you added it. Remove the icon,
  fix the certificate, and add it again.
- **The offline screen doesn't appear; the browser's own error shows instead.** The app
  has to be opened once with the server reachable, so the phone can store the offline
  screen. Open it once while connected, then try again.
- **Asked to log in again after installing.** On some phones (iPhone in particular) the
  installed app keeps its own login, separate from the browser. Log in once inside the app.

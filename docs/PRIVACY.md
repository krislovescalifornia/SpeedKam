# Privacy & legal notes for operators

SpeedKam is a monitoring tool for **your own private road**. Running a camera
that records a roadway carries real privacy responsibilities, and they vary a
lot by country, state, and even municipality. This page is practical guidance to
help you deploy responsibly — **it is not legal advice.** If you record anywhere
people might reasonably expect privacy, or beyond your own property, check your
local laws (or ask a lawyer) before you switch it on.

## What SpeedKam stores

Be clear-eyed about the data you are creating:

- **Video clips and JPEG snapshots** of passing vehicles — which can incidentally
  capture number plates, faces, pedestrians, neighbours, and their comings and
  goings.
- A **CSV log** of every pass: timestamp, speed, direction, and (if recognition
  is on) inferred vehicle **type / make / model / year / colour**.
- If off-site backup is enabled, **a mirror of all of the above on a web host you
  control** (`deploy/webhost/`).

Even with no number-plate OCR, a fixed camera logging timestamped vehicle
descriptions is capable of building a pattern-of-life record. Treat it as
sensitive.

## Know your local rules

Depending on where you are, some of these may apply:

- **Lawful basis / consent.** Data-protection regimes (e.g. EU/UK GDPR) generally
  require a lawful basis to record identifiable people, and household/private use
  exemptions often **do not** extend to cameras pointed at public spaces.
- **Notice / signage.** Some jurisdictions require visible signs telling people
  they're being recorded.
- **Subject rights.** People may have rights to access or request deletion of
  footage of themselves.
- **Enforcement.** SpeedKam is **not** a calibrated legal-enforcement instrument.
  Don't present its readings as authoritative evidence or use footage against
  third parties without understanding the legal exposure.

## Deploy responsibly (practical checklist)

The software gives you controls — use them:

- **Aim tight.** Frame only the stretch of *your* road you need. The narrower the
  field of view, the less incidental capture of neighbours and public space.
- **Set retention.** Turn on `retention.enabled` with a short `retention.local_days`,
  and set `backup.remote_retention_days` for the off-site copy. Don't keep footage
  longer than you need. (The CSV counts survive media rotation, so you keep the
  statistics without hoarding video.)
- **Protect the dashboards.** Set `web.auth.password` if the camera shares a
  network with anyone you don't fully trust (see the README), and always set a
  strong `$DASHBOARD_PASSWORD` on the off-site host.
- **Encrypt in transit.** Use an `https://` backup URL so footage, the secret,
  and the password aren't sent in the clear.
- **Lock down the off-site host.** Keep `speedkam_data/` non-public (the shipped
  `.htaccess` does this); media is served only through the authenticated proxy.
- **Keep secrets out of git.** Real credentials live in `config.local.yaml` and
  the untracked `speedkam_config.php`, never in the tracked config.
- **Minimise recognition.** Only enable `recognition.enabled` if you actually
  need vehicle attributes; it infers and stores more about each vehicle.

## Not legal advice

This document is a starting point, not a compliance guarantee. Laws change and
differ by location. You are responsible for how you deploy SpeedKam.

# Security policy

Floorgraph routes every change to the shop floor through one approval-gated action layer, and it
accepts machine events through a webhook that has no Frappe session. Both are security boundaries,
so reports about them are especially welcome:

- the machine event webhook (HMAC-SHA256 over the raw request body, per-source allowlist)
- the action layer: permissions, approval rules, and the `source` of an action (Human, Sensor, Agent)
- agent rules, which must never run arbitrary code or skip human approval

If you find a security problem, please do not open a public issue. Email
sidharth.thamban@gmail.com with the Frappe version, the steps to reproduce and what you saw. You will
get an answer within a few working days, and I ask for time to ship a fix before details are shared.

Supported versions: the latest release on the `main` branch (Frappe v15 and v16).

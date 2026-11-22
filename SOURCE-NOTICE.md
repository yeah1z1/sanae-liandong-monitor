# Public source scope and project notice

This is a generic, self-hosted, read-only shop catalog monitor. The public tree contains source code, offline tests, and synthetic/de-identified fixtures only. It contains no private deployment configuration, credentials, personal contact or group targets, runtime state, delivery receipts, or machine-specific paths.

Fixture store names, IDs, product identifiers, and titles are synthetic. The monitor requests only the public catalog APIs configured by the operator; an optional provider-specific adapter may read a customer token from an operator-supplied file at runtime. No purchase or account-management operations are implemented.

No third-party frontend source, binaries, cookies, or personal account data are included. The optional Excel exporter is project code and requires the operator to provide Node.js and `@oai/artifact-tool` separately.

No open-source license is included at this time. Third-party names, domains, APIs, and trademarks remain the property of their respective owners.

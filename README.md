# zuno_register

Synapse module gating Zuno Chat signup behind proof of inbox control: the app posts an email address, the module mints a single-use Matrix registration token straight into Synapse's store and emails it via Brevo. The endpoint is `POST /_synapse/client/zuno/register/token`, public by nature. The per-address record lives in the Redis Synapse already uses, so a resend delivers the same live code and nothing about the account ever holds the address.

- Design and request flow: [docs/design.md](docs/design.md)
- Config: the `modules:` entry in `homeserver.yaml`, documented in docs/design.md
- Develop: `make check` (ruff, mypy, unit tests); `make e2e` runs the module inside a real Synapse in Docker

## License

Free software under the GNU Affero General Public License, version 3 or any later version. See [LICENSE](LICENSE).

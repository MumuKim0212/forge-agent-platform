"""the one admin DSN a test uses when it needs `service_postgres_admin_dsn` to be set to a
syntactically valid value that must NEVER connect to anything.

Why a shared constant, and why this value. The suite used to spell this DSN at the dev service
Postgres's own published port (the developer's live instance, which `dev.ps1` starts and which holds the
demo service's database), with the real admin role name. A test that let code under test reach the
network (an orphan sweep did) connected to a real instance and failed only because the
fake password was wrong. So the value is chosen so that no listener can ever answer it:

- the host is under `.invalid` (RFC 2606 / RFC 6761: a name that is guaranteed never to resolve), not a
  loopback address. Nothing on the machine, the dev instance included, can be at that address. A
  loopback address with a "closed" port is not the same thing: measured on the Windows dev box, a
  connect to 127.0.0.1:1 does not refuse, it hangs until `connect_timeout` (2 s) and, with no
  timeout in the DSN, for 130 s. The `.invalid` name fails in about 10 ms with no timeout at all;
- the role is `forge_fake_admin`, not the dev admin role, so even a server that somehow answered
  would have no such login;
- `connect_timeout=2` stays in the DSN as a bound, for a resolver that hijacks NXDOMAIN.

`tests/test_dev_instance_guard.py` pins all three properties and connects once to prove the failure
is fast and is not an authentication failure (nothing was reached).
"""

FAKE_ADMIN_DSN = "postgresql://forge_fake_admin:test-pw@fake-admin-postgres.invalid:1/postgres?connect_timeout=2"

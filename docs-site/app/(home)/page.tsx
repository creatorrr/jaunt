import Link from 'next/link';

const SPEC_CODE = `from __future__ import annotations

import jaunt


@jaunt.magic()
def create_token(user_id: str, secret: str, ttl_seconds: int = 3600) -> str:
    """
    Create an HS256-signed JWT for a user.

    Payload: {"sub": user_id, "iat": <now>, "exp": <now + ttl_seconds>}
    - Sign with HMAC-SHA256 using \`secret\` as the key.
    - base64url segments must omit "=" padding.
    - Raise ValueError if user_id is empty.
    """
    ...`;

const GENERATED_CODE = `def create_token(user_id: str, secret: str, ttl_seconds: int = 3600) -> str:
    if user_id == "":
        raise ValueError("user_id is empty")

    now = int(time.time())
    header_segment = _json_segment({"alg": "HS256", "typ": "JWT"})
    payload_segment = _json_segment({"sub": user_id, "iat": now, "exp": now + ttl_seconds})
    signing_input = f"{header_segment}.{payload_segment}"
    signature = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature_segment = _base64url_encode(signature)
    return f"{signing_input}.{signature_segment}"`;

const FEATURES: { title: string; body: string }[] = [
  {
    title: 'Incremental builds',
    body: 'Jaunt hashes each spec with its dependencies and rebuilds only what actually changed. Reformatting or a comment edit rebuilds nothing.',
  },
  {
    title: 'Contract mode',
    body: 'Keep hand-written code canonical and let jaunt derive a committed pytest battery from its docstring. The inverse of magic, for code you already trust.',
  },
  {
    title: 'A CI gate that needs no API key',
    body: '`jaunt check` verifies generated code against its specs deterministically, so pull requests fail on drift without ever calling the model.',
  },
  {
    title: 'Built for coding agents',
    body: '`jaunt instructions` prints a project-aware primer, every command speaks `--json`, and a guard hook warns agents off editing generated files.',
  },
];

export default function HomePage() {
  return (
    <main className="flex flex-col">
      {/* Hero */}
      <section className="px-6 pt-20 pb-14 text-center">
        <p className="text-sm font-medium tracking-wide text-fd-muted-foreground">
          Spec-driven code generation for Python
        </p>
        <h1 className="mx-auto mt-3 max-w-3xl text-4xl font-bold sm:text-5xl">
          Describe what you want. Jaunt writes the Python.
        </h1>
        <p className="mx-auto mt-5 max-w-2xl text-fd-muted-foreground">
          Mark a function or class with <code>@jaunt.magic</code>, write the types and the
          docstring, and jaunt generates a real, reviewable implementation into your repo. It
          drives the OpenAI Codex CLI, the one supported engine, so you install Codex and run{' '}
          <code>codex login</code> first. After that it rebuilds incrementally as the spec changes.
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          <Link
            href="/docs/tutorials/quickstart"
            className="rounded-lg bg-fd-primary px-5 py-2.5 font-medium text-fd-primary-foreground"
          >
            Quickstart
          </Link>
          <a
            href="https://github.com/creatorrr/jaunt"
            rel="noreferrer noopener"
            target="_blank"
            className="rounded-lg border border-fd-border px-5 py-2.5 font-medium"
          >
            GitHub
          </a>
        </div>
        <div className="mt-6 flex justify-center">
          <a href="https://pypi.org/project/jaunt/" rel="noreferrer noopener" target="_blank">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="https://img.shields.io/pypi/v/jaunt" alt="jaunt version on PyPI" />
          </a>
        </div>
      </section>

      {/* Wow gap */}
      <section className="px-6 pb-16">
        <div className="mx-auto max-w-5xl">
          <h2 className="text-center text-xl font-semibold">Two docstrings in, working code out</h2>
          <p className="mx-auto mt-2 max-w-2xl text-center text-sm text-fd-muted-foreground">
            The spec on the left is what you commit. On the right is the function jaunt generated
            from it on a real run; it also wrote the small <code>_json_segment</code> and{' '}
            <code>_base64url_encode</code> helpers this calls. None of it is hand-edited.
          </p>
          <div className="mt-6 grid gap-4 md:grid-cols-2">
            <CodePanel label="Spec — you write this" code={SPEC_CODE} />
            <CodePanel label="Generated — jaunt writes this" code={GENERATED_CODE} />
          </div>
        </div>
      </section>

      {/* Feature cards */}
      <section className="border-t border-fd-border bg-fd-muted/30 px-6 py-16">
        <div className="mx-auto grid max-w-5xl gap-4 sm:grid-cols-2">
          {FEATURES.map((f) => (
            <div key={f.title} className="rounded-xl border border-fd-border bg-fd-card p-5">
              <h3 className="font-semibold">{f.title}</h3>
              <p className="mt-2 text-sm text-fd-muted-foreground">{f.body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Honest strip */}
      <section className="px-6 py-16">
        <div className="mx-auto max-w-3xl rounded-xl border border-fd-border p-6 text-center">
          <h2 className="text-lg font-semibold">What it costs, where it breaks</h2>
          <p className="mx-auto mt-2 max-w-xl text-sm text-fd-muted-foreground">
            Generation spends real tokens, and jaunt is a poor fit for throwaway scripts or
            hot-path code you would hand-tune anyway. The trade-offs are written down before you
            commit to them.
          </p>
          <div className="mt-5 flex flex-wrap items-center justify-center gap-4 text-sm font-medium">
            <Link href="/docs/concepts/design-philosophy" className="underline">
              When to reach for jaunt
            </Link>
            <Link href="/docs/reference/limitations" className="underline">
              Known limitations
            </Link>
          </div>
        </div>
      </section>
    </main>
  );
}

function CodePanel({ label, code }: { label: string; code: string }) {
  return (
    <div className="overflow-hidden rounded-xl border border-fd-border bg-fd-card">
      <div className="border-b border-fd-border px-4 py-2 text-xs font-medium text-fd-muted-foreground">
        {label}
      </div>
      <pre className="overflow-x-auto p-4 text-xs leading-relaxed">
        <code>{code}</code>
      </pre>
    </div>
  );
}

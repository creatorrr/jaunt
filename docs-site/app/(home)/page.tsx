import Link from 'next/link';

const SPEC_CODE = `import re

import jaunt

jaunt.magic_module(__name__, prompt="All parsers are RFC 5322 strict.")

ADDR_RE = re.compile(r"[^@\\s]+@[^@\\s]+")   # handwritten constant


class Email:
    """An email message with from_, to, subject, and body string fields.

    Validate on construction: from_ and to must each look like an address.
    """


def parse_email(raw: str) -> Email:
    """Parse a raw RFC 5322 payload into an Email. Read the From, To, and
    Subject headers and the body after the first blank line. Raise ValueError
    on malformed input."""
    ...


def _render_debug(email: Email) -> str:      # real body → handwritten
    return f"<{email.from_} -> {email.to}>"`;

const GENERATED_CODE = `def parse_email(raw: str) -> Email:
    raw_text = _require_string("raw", raw)
    header_text, body = _split_headers_body(raw_text)
    headers = _parse_headers(header_text)

    from_ = _required_header(headers, "From", allow_empty=False)
    to = _required_header(headers, "To", allow_empty=False)
    subject = _required_header(headers, "Subject", allow_empty=True)

    try:
        return Email(from_, to, subject, body)
    except ValueError as exc:
        raise ValueError(f"Malformed email: {exc}") from exc`;

type Feature = { title: string; body: string; href?: string };

const FEATURES: Feature[] = [
  {
    title: 'Parallel, DAG-scheduled builds',
    body: 'Modules build over the dependency graph, critical path first. A module starts the instant its dependencies finish, up to `[build] jobs` at once, and a failure skips only its dependents. No wave barriers.',
    href: '/docs/concepts/how-jaunt-works#parallel-builds',
  },
  {
    title: 'Change detection that reads the contract',
    body: 'Digests hash the AST-normalized contract, so reformatting and comment edits rebuild nothing. Staleness follows the graph, and a small judge re-freezes reworded docstrings instead of paying for a rebuild.',
    href: '/docs/concepts/change-detection',
  },
  {
    title: 'Contract mode',
    body: 'Keep hand-written code canonical and let jaunt derive a committed pytest battery from its docstring. The inverse of magic, for code you already trust.',
    href: '/docs/guides/contract-mode',
  },
  {
    title: 'Built for coding agents',
    body: '`jaunt instructions` prints a project-aware primer, every command speaks `--json`, `jaunt check` gates drift with no API key, and a guard hook warns agents off editing generated files.',
    href: '/docs/guides/coding-agents',
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
          Call <code>jaunt.magic_module(__name__)</code> at the top of a file and every typed stub
          below it becomes a spec. Jaunt reads the signatures and docstrings and generates real,
          reviewable Python into your repo. Need per-symbol control? Decorate just that symbol with{' '}
          <code>@jaunt.magic</code>. It drives the OpenAI Codex CLI, the one supported engine, so you
          install Codex and run <code>codex login</code> first.
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
          <h2 className="text-center text-xl font-semibold">A module of intent in, working code out</h2>
          <p className="mx-auto mt-2 max-w-2xl text-center text-sm text-fd-muted-foreground">
            The module on the left is what you commit: one <code>magic_module</code> call, a
            docstring-only <code>Email</code> class, a function stub, and a handwritten helper, all
            in one file. On the right is the <code>parse_email</code> jaunt generated on a real run.
            It also designed the <code>Email</code> class and the header-parsing helpers this calls,
            and left <code>ADDR_RE</code> and <code>_render_debug</code> alone. None of it is
            hand-edited.
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
              <p className="mt-2 text-sm text-fd-muted-foreground">{renderInlineCode(f.body)}</p>
              {f.href ? (
                <Link href={f.href} className="mt-3 inline-block text-sm font-medium underline">
                  Learn more
                </Link>
              ) : null}
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

function renderInlineCode(text: string) {
  return text.split('`').map((part, i) =>
    i % 2 === 1 ? (
      <code key={i}>{part}</code>
    ) : (
      <span key={i}>{part}</span>
    ),
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

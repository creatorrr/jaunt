import Link from 'next/link';

export default function HomePage() {
  return (
    <div className="flex flex-col justify-center text-center flex-1 px-6">
      <h1 className="text-4xl font-bold mb-4">Jaunt</h1>
      <p className="text-fd-muted-foreground max-w-2xl mx-auto">
        Spec-driven code generation for Python: write intent with <code>@jaunt.magic</code> and{' '}
        <code>@jaunt.test</code>, then generate real modules under <code>__generated__/</code>.
      </p>
      <div className="mt-8 flex items-center justify-center gap-4">
        <Link href="/docs" className="font-medium underline">
          Read the docs
        </Link>
        <Link href="/docs/getting-started" className="font-medium underline">
          Getting started
        </Link>
        <a
          className="font-medium underline"
          href="https://github.com/ijindal1/jaunt"
          rel="noreferrer noopener"
          target="_blank"
        >
          GitHub
        </a>
      </div>
    </div>
  );
}

import { getLLMText, source } from '@/lib/source';

export const dynamic = 'force-static';

export async function GET() {
  // Redirect stubs for moved slugs carry the title "Moved" — skip them.
  const pages = source.getPages().filter((page) => page.data.title !== 'Moved');
  const texts = await Promise.all(pages.map(getLLMText));

  return new Response(texts.join('\n\n---\n\n'), {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
}

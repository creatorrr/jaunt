'use client';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

export function Moved({ to }: { to: string }) {
  const router = useRouter();
  const external = /^https?:\/\//.test(to);
  useEffect(() => {
    if (external) {
      window.location.replace(to);
    } else {
      router.replace(to);
    }
  }, [router, to, external]);
  return (
    <p>
      This page moved to <a href={to}>{to}</a>.
    </p>
  );
}

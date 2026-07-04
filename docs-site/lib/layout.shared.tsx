import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';

export function baseOptions(): BaseLayoutProps {
  return {
    githubUrl: 'https://github.com/creatorrr/jaunt',
    nav: {
      title: 'Jaunt',
    },
    links: [
      { text: 'GitHub', url: 'https://github.com/creatorrr/jaunt', external: true },
      { text: 'PyPI', url: 'https://pypi.org/project/jaunt/', external: true },
    ],
  };
}

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{html,js,svelte,ts}'],
  theme: {
    extend: {
      colors: {
        surface: {
          900: '#0d0f1a',
          800: '#131623',
          700: '#1a1d2e',
          600: '#222539',
          500: '#2d3050',
        },
        border: '#2a2d42',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
};

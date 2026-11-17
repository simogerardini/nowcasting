import type { Config } from "tailwindcss";

export default {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        night: { 900: "#0b1020", 800: "#121a33", 700: "#1b2547" },
        sev: { moderate: "#facc15", severe: "#f97316", extreme: "#dc2626" },
      },
    },
  },
  plugins: [],
} satisfies Config;

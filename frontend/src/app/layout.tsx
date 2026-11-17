import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Noctalix Nowcast — Allerta temporali in tempo reale",
  description: "Radar e nowcasting a 60 minuti con avvisi push geolocalizzati per nubifragi, grandine e temporali.",
  manifest: "/manifest.json",
  appleWebApp: { capable: true, title: "Nowcast", statusBarStyle: "black-translucent" },
  icons: { icon: "/icons/icon-192.png", apple: "/icons/apple-touch-icon.png" },
};

export const viewport: Viewport = {
  themeColor: "#0b1020",
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it">
      <body className="antialiased">{children}</body>
    </html>
  );
}

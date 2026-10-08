import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = { title: "Smart Money Radar", description: "Solana wallet activity and cluster signals" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}

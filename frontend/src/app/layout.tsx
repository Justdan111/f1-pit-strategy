import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "F1 Pit Strategy",
  description: "Live pit/stay decisions streamed over a WebSocket.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

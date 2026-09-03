import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "hbday-zee",
  description: "Story to video, one supervised stage at a time.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

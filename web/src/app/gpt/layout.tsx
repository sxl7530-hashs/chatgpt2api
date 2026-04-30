import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "xingjiabiapi.org gpt-image-2 1k体验",
  description: "xingjiabiapi.org gpt-image-2 1k体验",
};

export default function GptLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return children;
}

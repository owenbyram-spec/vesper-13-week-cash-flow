import "./globals.css";
import { Providers } from "./providers";
import { LocalShell } from "@/components/local-shell";

export const metadata = { title: "Vesper & Co · Cash Flow", description: "Local cash-flow workspace using the PE Workflow Builder platform foundation." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
    return <html lang="en" suppressHydrationWarning><body className="bg-background text-text"><Providers><LocalShell>{children}</LocalShell></Providers></body></html>;
}

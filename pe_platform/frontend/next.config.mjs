/** @type {import('next').NextConfig} */
const nextConfig = {
    output: 'standalone',
    async headers() {
        const development = process.env.NODE_ENV === 'development';
        const policy = [
            "default-src 'self'",
            `script-src 'self' 'unsafe-inline'${development ? " 'unsafe-eval'" : ''}`,
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            `connect-src 'self'${development ? ' ws://127.0.0.1:* ws://localhost:*' : ''}`,
            "font-src 'self'",
            "object-src 'none'",
            "frame-src 'none'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ].join('; ');
        // Local privacy adapter: native assistant Markdown cannot auto-load
        // remote images or create an additional browser data-transfer path.
        return [{ source: '/:path*', headers: [
            { key: 'Content-Security-Policy', value: policy },
            { key: 'Referrer-Policy', value: 'no-referrer' },
            { key: 'X-Content-Type-Options', value: 'nosniff' },
            { key: 'X-Frame-Options', value: 'DENY' },
        ] }];
    },
};

export default nextConfig;

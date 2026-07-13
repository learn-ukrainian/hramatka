import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';
import fs from 'fs';

function resolveActivityKit(): string {
  const envDir = process.env.ACTIVITY_KIT_DIR;
  if (envDir && fs.existsSync(envDir)) return path.resolve(envDir);
  const candidates = [
    path.resolve(__dirname, '../../public/packages/activity-kit'),
    path.resolve(__dirname, '../../../../../../../learn-ukrainian/packages/activity-kit'),
  ];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return path.resolve(process.cwd(), '../learn-ukrainian/packages/activity-kit');
}
const activityKitSrc = path.join(resolveActivityKit(), 'src');

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@learn-ukrainian/activity-kit$': path.join(activityKitSrc, 'index.ts'),
      '@learn-ukrainian/activity-kit/styles.css': path.join(activityKitSrc, 'components/Activities.module.css'),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
});

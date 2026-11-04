import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({plugins:[react()],resolve:{alias:{"@":"/src"}},build:{rollupOptions:{output:{manualChunks:{charts:['recharts'],tree:['@xyflow/react']}}}},server:{proxy:{'/api':'http://api:8000'}}});

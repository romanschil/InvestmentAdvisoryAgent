# Frontend Dockerfile (build + serve using vite preview)
FROM node:18
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ .
RUN npm run build
EXPOSE 3000
CMD ["npm","run","preview","--","--port","3000","--host"]

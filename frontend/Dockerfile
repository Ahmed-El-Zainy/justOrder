FROM node:22-alpine

WORKDIR /srv

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 4200

# --host 0.0.0.0 is required for the port to be reachable outside the container.
CMD ["npx", "ng", "serve", "--host", "0.0.0.0", "--port", "4200", "--poll", "2000"]

/**
 * Demo consumer — plain `node src/app.ts`, no hooks, no jaunt runtime.
 *
 * Everything is reached through the ordinary public facade
 * (./tokens/index.ts): one module graph, standard resolution. If the module
 * had never been built, the facade's import of __generated__/impl.ts would
 * be a compile/check failure — unbuilt state surfaces at typecheck time,
 * not as a runtime mystery.
 */
import { TokenStore, createToken, rotateToken, verifyToken } from "./tokens/index.ts";

const secret = "s3cret";

const token = createToken("user-42", secret, { ttlSeconds: 60 });
const claims = verifyToken(token, secret);
console.log(`issued for ${claims.sub}: iat=${claims.iat} exp=${claims.exp}`);

const rotated = rotateToken(token, secret);
const rotatedClaims = verifyToken(rotated, secret);
console.log(`rotated: iat=${rotatedClaims.iat} (strictly > ${claims.iat})`);

const store = new TokenStore();
store.put(rotatedClaims.sub, rotated, rotatedClaims.exp);
console.log(
  `store holds ${store.size} live token(s); user-42 -> ${store.get("user-42") === rotated ? "rotated token" : "??"}`,
);

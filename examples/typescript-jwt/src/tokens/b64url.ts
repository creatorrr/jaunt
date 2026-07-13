/**
 * base64url (RFC 4648 §5, unpadded) codec — a *contract-mode* module: the
 * committed code is canonical, the `@jauntContract` tag marks each adopted
 * symbol (written by `jaunt adopt`, the TS analog of Python's no-op
 * `@jaunt.contract` decorator), and jaunt derives a committed battery from
 * the TSDoc contract (tests/contract/b64url.contract.test.ts). Standard
 * tags replace Python's prose-section conventions: `@example` and `@throws`
 * are real JSDoc, and `@prop` carries a fast-check property bullet.
 */

/**
 * Encode bytes as unpadded base64url.
 *
 * @example encode(new Uint8Array([104, 105])) // => "aGk"
 * @example encode(new Uint8Array([])) // => ""
 * @prop given bytes: fc.uint8Array() :: decode(encode(bytes)) equals bytes
 * @jauntContract
 */
export function encode(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64url");
}

/**
 * Decode an unpadded base64url string.
 *
 * @example decode("aGk") // => Uint8Array [104, 105]
 * @throws TypeError if `text` contains characters outside [A-Za-z0-9_-]
 *   (including "=" padding), or has an impossible unpadded length
 *   (length % 4 === 1), which Buffer would otherwise decode silently.
 * @jauntContract
 */
export function decode(text: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) {
    throw new TypeError("invalid base64url");
  }
  return new Uint8Array(Buffer.from(text, "base64url"));
}

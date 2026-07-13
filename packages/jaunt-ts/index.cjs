"use strict";

const DEFAULT_MESSAGE =
  "Jaunt TypeScript spec modules are static analysis inputs and must not be executed. " +
  "Run `jaunt build` and import the module's public facade instead.";

class JauntNotBuiltError extends Error {
  constructor(message = DEFAULT_MESSAGE) {
    super(message);
    this.name = "JauntNotBuiltError";
    this.code = "JAUNT_NOT_BUILT";
  }
}

function fail() {
  throw new JauntNotBuiltError();
}

function magicModule() {
  fail();
}

function magic() {
  fail();
}

function testSpec() {
  fail();
}

module.exports = { JauntNotBuiltError, magicModule, magic, testSpec };

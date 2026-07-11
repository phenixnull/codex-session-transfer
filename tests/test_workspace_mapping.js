const assert = require('node:assert/strict');
const mapping = require('../static/workspace-mapping.js');

const manifest = {
  projects: [
    {
      cwd: 'C:\\old\\ProjectA',
      label: 'ProjectA',
      threads: [{ id: 'thread-a' }, { id: 'thread-a-child' }],
    },
    {
      cwd: '/Users/source/ProjectB/',
      label: 'ProjectB',
      threads: [{ id: 'thread-b' }],
    },
  ],
};

assert.deepEqual(
  mapping.selectedProjects(manifest, new Set(['thread-a', 'thread-b'])).map((project) => project.cwd),
  ['C:\\old\\ProjectA', '/Users/source/ProjectB/'],
);
assert.deepEqual(
  mapping.selectedProjects(manifest, new Set(['not-in-package'])),
  [],
);

assert.equal(mapping.projectLabel('C:\\old\\ProjectA\\'), 'ProjectA');
assert.equal(mapping.projectLabel('/Users/source/ProjectB/'), 'ProjectB');
assert.equal(mapping.joinPath('D:\\CodexWork\\', 'ProjectA'), 'D:\\CodexWork\\ProjectA');
assert.equal(mapping.joinPath('/Users/target/', 'ProjectB'), '/Users/target/ProjectB');

assert.equal(
  mapping.computedTarget('D:\\CodexWork', 'C:\\old\\ProjectA', 'preserve_projects'),
  'D:\\CodexWork\\ProjectA',
);
assert.equal(
  mapping.computedTarget('D:\\CodexWork', 'C:\\old\\ProjectA', 'single_workspace'),
  'D:\\CodexWork',
);

const effective = mapping.effectiveMappings(
  manifest.projects,
  'D:\\CodexWork',
  'preserve_projects',
  { 'C:\\old\\ProjectA': 'D:\\Renamed\\ProjectA' },
);
assert.deepEqual(
  effective.map((entry) => ({ source: entry.sourceCwd, target: entry.targetCwd, overridden: entry.overridden })),
  [
    { source: 'C:\\old\\ProjectA', target: 'D:\\Renamed\\ProjectA', overridden: true },
    { source: '/Users/source/ProjectB/', target: 'D:\\CodexWork\\ProjectB', overridden: false },
  ],
);

assert.deepEqual(
  mapping.duplicateTargets([
    { sourceCwd: 'C:\\one\\Same', targetCwd: 'D:\\target\\Same' },
    { sourceCwd: 'C:\\two\\Same', targetCwd: 'd:/target/same/' },
  ]),
  ['D:\\target\\Same'],
);
assert.deepEqual(
  mapping.duplicateTargets([
    { sourceCwd: '/one/Same', targetCwd: '/Target/Same' },
    { sourceCwd: '/two/Same', targetCwd: '/target/same' },
  ]),
  [],
);

console.log('workspace mapping tests passed');

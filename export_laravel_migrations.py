# -*- coding: utf-8 -*-
"""Workbench 8 -> Laravel anonymous migrations (Laravel >= 11), MySQL only.

Keeps export history in laravel_workbench_history.json. Commit that file together
with migrations. The snapshot tracks EXPORTED state, not the live database.
For an existing project, export its CURRENT model as baseline before editing it.
Never overwrites migrations. Review generated migrations before running artisan migrate.
Column/table removal and rollback cannot restore deleted data.
"""

import datetime
import glob
import json
import os
import re
import tempfile

import grt
import mforms
from wb import DefineModule, wbinputs
from workbench.ui import WizardForm, WizardPage

HISTORY = 'laravel_workbench_history.json'
FORMAT = 1

def ident(value):
    return '`' + str(value).replace('`', '``') + '`'

def php(value):
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"

def literal(value):
    return "'" + str(value).replace('\\', '\\\\').replace("'", "''") + "'"

def columns_sql(columns):
    return ', '.join(ident(c) for c in columns)


# Build the full MySQL definition of a Workbench column. This string is what
# the snapshot stores and what later exports compare to detect changes.
def column_sql(col):

    # formattedType is supplied by Workbench and preserves enum/set parameters,
    # lengths, precision and unsigned. No increments() inference from PK status.
    datatype = str(getattr(col, 'formattedType', '') or '').strip()
    if not datatype:
        raise ValueError('Workbench did not provide formattedType for ' + col.name)
    
    # Append UNSIGNED/ZEROFILL flags when formattedType does not include them.
    flags = [str(f).upper() for f in col.flags]
    for flag in ('UNSIGNED', 'ZEROFILL'):
        if flag in flags and not re.search(r'\b' + flag + r'\b', datatype, re.I):
            datatype += ' ' + flag
    result = ident(col.name) + ' ' + datatype

    charset = getattr(col, 'characterSetName', '')
    collation = getattr(col, 'collationName', '')
    if charset:
        result += ' CHARACTER SET ' + ident(charset)
    if collation:
        result += ' COLLATE ' + ident(collation)
  
    result += ' NOT NULL' if col.isNotNull else ' NULL'

    default = str(col.defaultValue or '')
    if col.defaultValueIsNull:
        result += ' DEFAULT NULL'
    elif default:

        if re.match(r'^NULL\s+ON\s+UPDATE\b', default, re.I):
            result += ' DEFAULT ' + default
        else:
            result += ' DEFAULT ' + default

    if getattr(col, 'autoIncrement', 0):
        result += ' AUTO_INCREMENT'
    if getattr(col, 'generated', 0):
        raise ValueError('Generated column unsupported: ' + col.name)

    result += ' COMMENT ' + literal(col.comment or '')
    return result


# Convert the Workbench catalog into a plain dict (the "snapshot") describing
# every table: columns, column order, indexes, foreign keys and table options.
def snapshot(catalog):

    schemas =[s for s in catalog.schemata if len(s.tables)]
    if len(schemas) != 1:
        raise ValueError('Export one non-empty schema at a time (one Laravel connection).')
    schema = schemas[0]
    tables = {}
    
    for tbl in sorted(schema.tables, key=lambda t: t.name):
        
        indices = {}
        for idx in tbl.indices:
            
            parts = []
            for part in idx.columns:
                sql = ident(part.referencedColumn.name)
                if getattr(part, 'columnLength', 0) > 0:
                    sql += '(' + str(part.columnLength) + ')'
                if getattr(part, 'descend', 0):
                    sql += ' DESC'
                parts.append(sql)
                
            kind = str(idx.indexType).upper()
            if idx.isPrimary:
                name, prefix = 'PRIMARY', 'PRIMARY KEY'
            else:
                name = idx.name
                prefix = {'UNIQUE': 'UNIQUE INDEX', 'INDEX': 'INDEX',
                          'FULLTEXT': 'FULLTEXT INDEX', 'SPATIAL': 'SPATIAL INDEX'}.get(kind)
                if prefix is None:
                    raise ValueError('Unsupported index type: ' + kind)
                prefix += ' ' + ident(name)
            indices[name] = prefix + ' (' + ', '.join(parts) + ')'
            
        foreign = {}
        for key in tbl.foreignKeys:
            if not key.name or not key.referencedTable:
                raise ValueError('Foreign keys must be named and have a referenced table.')
            if key.referencedTable.owner.name != schema.name:
                raise ValueError('Cross-schema foreign keys unsupported: ' + key.name)
            
            for local, remote in zip(key.columns, key.referencedColumns):
                lt = re.sub(r'\(\d+\)', '', column_sql(local).split(' COMMENT ')[0])
                rt = re.sub(r'\(\d+\)', '', column_sql(remote).split(' COMMENT ')[0])
                
                pattern = r'\b(TINYINT|SMALLINT|MEDIUMINT|BIGINT|INT|INTEGER)\b'
                lm, rm = re.search(pattern, lt, re.I), re.search(pattern, rt, re.I)
                if lm and rm and (lm.group(1).upper().replace('INTEGER', 'INT'),
                                   'UNSIGNED' in lt.upper() or 'ZEROFILL' in lt.upper()) != (
                                   rm.group(1).upper().replace('INTEGER', 'INT'),
                                   'UNSIGNED' in rt.upper() or 'ZEROFILL' in rt.upper()):
                    raise ValueError('Incompatible integer foreign key: ' + key.name)
                
            foreign[key.name] = ('CONSTRAINT ' + ident(key.name) + ' FOREIGN KEY (' +
                columns_sql([c.name for c in key.columns]) + ') REFERENCES ' +
                ident(key.referencedTable.name) + ' (' +
                columns_sql([c.name for c in key.referencedColumns]) + ')' +
                ' ON DELETE ' + (key.deleteRule or 'RESTRICT') +
                ' ON UPDATE ' + (key.updateRule or 'RESTRICT'))
            
        options = 'ENGINE=' + ident(tbl.tableEngine or 'InnoDB')
        if getattr(tbl, 'defaultCharacterSetName', ''):
            options += ' DEFAULT CHARACTER SET ' + ident(tbl.defaultCharacterSetName)
        if getattr(tbl, 'defaultCollationName', ''):
            options += ' COLLATE ' + ident(tbl.defaultCollationName)
        options += ' COMMENT=' + literal(tbl.comment or '')
        
        tables[tbl.name] = {'columns': {c.name: column_sql(c) for c in tbl.columns},
                            'order': [c.name for c in tbl.columns],
                            'indices': indices, 'foreign': foreign, 'options': options}
    return {'format': FORMAT, 'schema': schema.name, 'tables': tables}


def sql_tokens(sql):
    return re.findall(r"`(?:``|[^`])*`|'(?:''|\\.|[^'\\])*'|[A-Za-z_][A-Za-z_0-9]*|[-+]?\d+(?:\.\d+)?|[^\s]", sql)


# Remove the quoting of a token: backtick identifiers and SQL strings (with
# their escape sequences) return the raw value; other tokens are unchanged.
def unquote(token):
    if token.startswith('`'):
        return token[1:-1].replace('``', '`')
    if token.startswith("'"):
        return re.sub(r'\\(.)', lambda m: {'n': '\n', 'r': '\r', 't': '\t', '0': '\0', 'b': '\b', 'Z': '\x1a'}.get(m.group(1), m.group(1)), token[1:-1].replace("''", "'"))
    return token


# Render a list of strings as a PHP array literal.
def arr(values):
    return '[' + ', '.join(php(v) for v in values) + ']'


# Split a stored column definition into name, base type, type parameters and
# the remaining tokens (nullability, default, comment, ...).
def parsed_column(sql):
    tokens = sql_tokens(sql)
    name, kind = unquote(tokens[0]), tokens[1].upper()
    
    pos, params = 2, []
    if pos < len(tokens) and tokens[pos] == '(':
        pos += 1
        while tokens[pos] != ')':
            if tokens[pos] != ',':
                params.append(unquote(tokens[pos]))
            pos += 1
        pos += 1
    return name, kind, params, tokens[pos:]


# Translate a stored column definition into a Laravel Blueprint statement.
# `change` marks an existing column; `position` is the preceding column name
# or '__FIRST__'.
def blueprint_column(sql, change=False, position=None):
    name, kind, params, tail = parsed_column(sql)
    
    upper =[t.upper() if not t.startswith(("'", '`')) else t for t in tail]
    
    integers = {'TINYINT': 'tinyInteger', 'SMALLINT': 'smallInteger',
                'MEDIUMINT': 'mediumInteger', 'INT': 'integer', 'INTEGER': 'integer',
                'BIGINT': 'bigInteger', 'INT1': 'tinyInteger', 'INT2': 'smallInteger',
                'INT3': 'mediumInteger', 'INT4': 'integer', 'INT8': 'bigInteger'}
    types = {'VARCHAR': 'string', 'CHAR': 'char', 'TEXT': 'text', 'MEDIUMTEXT': 'mediumText',
             'LONGTEXT': 'longText', 'JSON': 'json', 'DATE': 'date', 'DATETIME': 'dateTime',
             'TIME': 'time', 'TIMESTAMP': 'timestamp', 'YEAR': 'year',
             'DECIMAL': 'decimal', 'NUMERIC': 'decimal', 'ENUM': 'enum', 'SET': 'set',
             'DOUBLE': 'double', 'FLOAT': 'float', 'BOOL': 'boolean', 'BOOLEAN': 'boolean',
             'BLOB': 'binary'}
    
    if 'ZEROFILL' in upper:
        raise ValueError('ZEROFILL has no exact Blueprint equivalent: ' + name)
    
    args = [php(name)]
    ai = 'AUTO_INCREMENT' in upper
    unsigned = 'UNSIGNED' in upper
    if kind in integers:
        method = integers[kind]
        args += ['true' if ai else 'false', 'true' if unsigned else 'false']
    elif kind in types:
        method = types[kind]
        if ai:
            raise ValueError('Auto increment requires an integer: ' + name)
        if kind in ('VARCHAR', 'CHAR'):
            args += [str(int(params[0]))] if params else []
        elif kind in ('DECIMAL', 'NUMERIC'):
            args += [str(int(p)) for p in params]
        elif kind in ('ENUM', 'SET'):
            args += [arr(params)]
        elif kind in ('DATETIME', 'TIMESTAMP', 'TIME'):
            args += [str(int(params[0]))] if params else []
        elif kind in ('FLOAT', 'DOUBLE') and params:
            raise ValueError('FLOAT/DOUBLE parameters vary by Laravel version: ' + name)
    else:
        raise ValueError('No exact supported Blueprint mapping for ' + kind + ': ' + name)
    
    output = '$table->' + method + '(' + ', '.join(args) + ')'
    if unsigned and kind not in integers:
        output += '->unsigned()'
    output += '->nullable(false)' if 'NOT' in upper else '->nullable()'
    
    for keyword, modifier in [('CHARACTER', 'charset'),('COLLATE', 'collation'), ('COMMENT', 'comment')]:
        if keyword in upper:
            offset = 2 if keyword == 'CHARACTER' else 1
            output += '->' + modifier + '(' + php(unquote(tail[upper.index(keyword) + offset])) + ')'
            
    if 'DEFAULT' in upper:
        start = upper.index('DEFAULT') + 1
        stop = next((i for i in range(start, len(tail)) if upper[i] in ('ON', 'AUTO_INCREMENT', 'COMMENT')), len(tail))
        default_tokens = tail[start:stop]
        if len(default_tokens) == 1 and default_tokens[0].startswith("'"):
            default = php(unquote(default_tokens[0]))
        elif len(default_tokens) == 1 and default_tokens[0].upper() == 'NULL':
            default = 'null'
        elif len(default_tokens) == 1 and re.fullmatch(r'[-+]?\d+(?:\.\d+)?', default_tokens[0]):
            default = php(default_tokens[0])
        else:
            default = 'DB::raw(' + php(' '.join(default_tokens)) + ')'
        output += '->default(' + default + ')'

    if 'ON' in upper:
        i = upper.index('ON')
        if upper[i:i+3] != ['ON', 'UPDATE', 'CURRENT_TIMESTAMP']:
            raise ValueError('Unsupported ON UPDATE expression: ' + name)
        if kind not in ('TIMESTAMP', 'DATETIME'):
            raise ValueError('ON UPDATE requires a timestamp/dateTime column: ' + name)
        output += '->useCurrentOnUpdate()'
        
    if position:
        output += '->first()' if position == '__FIRST__' else '->after(' + php(position) + ')'
    if change:
        output += '->change()'
    return output + ';'


# Extract the column names from a stored index definition.
def index_parts(sql):
    tokens = sql_tokens(sql)
    start = tokens.index('(')
    
    inner = tokens[start+1:-1]
    if any(t != ',' and not t.startswith('`') for t in inner):
        raise ValueError('Prefix/descending index needs a manual migration: ' + sql)
    return [unquote(t) for t in inner if t != ',']


# Build the Blueprint statement that adds (or, with drop=True, drops) an index.
def blueprint_index(key, sql, drop=False):
    
    kind = sql.split()[0].upper()
    methods = {'PRIMARY': ('primary', 'dropPrimary'), 'UNIQUE': ('unique', 'dropUnique'),
               'INDEX': ('index', 'dropIndex'), 'FULLTEXT': ('fullText', 'dropFullText'),
               'SPATIAL': ('spatialIndex', 'dropSpatialIndex')}
    
    method = methods[kind][1 if drop else 0]
    if drop:
        return '$table->' + method + '(' + ('' if key == 'PRIMARY' else php(key)) + ');'
    return ('$table->' + method + '(' + arr(index_parts(sql)) +
            ('' if key == 'PRIMARY' else ', ' + php(key)) + ');')


# Convert stored foreign key SQL into a Blueprint foreign() chain.
def blueprint_foreign(key, sql):
    tokens = sql_tokens(sql)
    
    start = tokens.index('(')
    end = tokens.index(')', start)
    local = [unquote(t) for t in tokens[start+1:end] if t != ',']
    ref = tokens.index('REFERENCES')
    remote_end = tokens.index(')', ref)
    remote = [unquote(t) for t in tokens[ref+3:remote_end] if t != ',']
    
    delete = tokens.index('DELETE')
    update = tokens.index('UPDATE')
    delete_rule = ' '.join(tokens[delete+1:update-1]).lower()
    update_rule = ' '.join(tokens[update+1:]).lower()
    return ('$table->foreign(' + arr(local) + ', ' + php(key) + ')'
            '->references(' + arr(remote) + ')->on(' + php(unquote(tokens[ref+1])) + ')'
            '->onDelete(' + php(delete_rule) + ')->onUpdate(' + php(update_rule) + ');')


# Wrap Blueprint lines in a Schema::create/Schema::table closure. Returns ''
# when there is nothing to emit.
def schema_block(name, lines, create=False):
    if not lines:
        return ''
    return ('        Schema::' + ('create' if create else 'table') + '(' + php(name) +
            ', function (Blueprint $table) {\n' +
            '\n'.join('            ' + line for line in lines) + '\n        });')


# Translate stored table options into Blueprint table properties.
def table_options(sql):
    tokens = sql_tokens(sql)
    lines = []
    
    for keyword, attribute, offset in [('ENGINE', 'engine', 2), ('CHARACTER', 'charset', 2),
                                        ('COLLATE', 'collation', 1)]:
        if keyword in tokens:
            lines.append('$table->' + attribute + ' = ' + php(unquote(tokens[tokens.index(keyword)+offset])) + ';')
            
    if 'COMMENT' in tokens:
        comment = unquote(tokens[tokens.index('COMMENT')+2])
        if comment:
            lines.append('$table->comment(' + php(comment) + ');')
    return lines


# Return (local columns, referenced table, referenced columns) of a stored
# foreign key definition.
def foreign_columns(sql):
    tokens = sql_tokens(sql)
    start = tokens.index('(')
    end = tokens.index(')', start)
    ref = tokens.index('REFERENCES')
    remote_end = tokens.index(')', ref)
    return ([unquote(t) for t in tokens[start+1:end] if t != ','],
            unquote(tokens[ref+1]),
            [unquote(t) for t in tokens[ref+3:remote_end] if t != ','])


# Decide whether an existing foreign key must be dropped before the other
# changes are applied (it is recreated afterwards if it still exists).
def foreign_affected(owner, key, before, after):
    old = before[owner]
    sql = old['foreign'][key]
    
    if owner not in after or after[owner]['foreign'].get(key) != sql:
        return True
    
    local, parent, remote = foreign_columns(sql)
    for name, columns in ((owner, local), (parent, remote)):
        if name not in before or name not in after:
            return True
        
        previous, current = before[name], after[name]
        if any(previous['columns'].get(c) != current['columns'].get(c) for c in columns):
            return True
        
        for index, definition in previous['indices'].items():
            if definition != current['indices'].get(index):
                if index_parts(definition)[:len(columns)] == columns:
                    return True
    return False


# Produce the ordered list of Schema blocks that turn the `before` tables into
# the `after` tables. Called in both directions to build up() and down().
def delta(before, after):
    if before == after:
        return []
    blocks = []
    
    affected = {(name, key) for name in before for key in before[name]['foreign']
                if foreign_affected(name, key, before, after)}
    for name in sorted(before):
        blocks.append(schema_block(name, ['$table->dropForeign(' + php(k) + ');'
                                        for k in sorted(before[name]['foreign']) if (name, k) in affected]))
        
    for name in sorted(set(before) - set(after)):
        blocks.append('        Schema::drop(' + php(name) + ');')
        
    for name in sorted(after):
        new = after[name]
        
        if name not in before:
            lines = table_options(new['options'])
            lines += [blueprint_column(new['columns'][c]) for c in new['order']]
            for key in sorted(new['indices']):
                
                lines.append(blueprint_index(key, new['indices'][key]))
            blocks.append(schema_block(name, lines, True))
            continue
        
        old = before[name]
        if new['options'] != old['options']:
            raise ValueError('Changing table engine/charset/comment requires a manual migration: ' + name)
        primary_changed = old['indices'].get('PRIMARY') != new['indices'].get('PRIMARY')
        if primary_changed and any('AUTO_INCREMENT' in sql_tokens(s) for s in
                                   list(old['columns'].values()) + list(new['columns'].values())):
            raise ValueError('Changing an auto-increment primary key requires a manual migration: ' + name)
        
        drops, additions = [], []
        for key in sorted(old['indices']):
            if old['indices'][key] != new['indices'].get(key):
                drops.append(blueprint_index(key, old['indices'][key], True))
        blocks.append(schema_block(name, drops))
        
        removed = [c for c in old['order'] if c not in new['columns']]
        blocks.append(schema_block(name, ['$table->dropColumn(' + arr(removed) + ');'] if removed else []))
        
        previous = None
        working_order = [c for c in old['order'] if c in new['columns']]
        for target_index, col in enumerate(new['order']):
            changed = col in old['columns'] and new['columns'][col] != old['columns'][col]
            added = col not in old['columns']
            moved = not added and working_order.index(col) != target_index
            if added or changed or moved:
                if col in old['columns'] and ('AUTO_INCREMENT' in sql_tokens(old['columns'][col])) != ('AUTO_INCREMENT' in sql_tokens(new['columns'][col])):
                    raise ValueError('Changing auto increment requires a manual migration: ' + name + '.' + col)
                
                position = (previous if previous else '__FIRST__') if added or moved else None
                blocks.append(schema_block(name, [blueprint_column(new['columns'][col],
                              change=col in old['columns'], position=position)]))
            if added or moved:
                if col in working_order:
                    working_order.remove(col)
                working_order.insert(target_index, col)
            previous = col
            
        for key in sorted(new['indices']):
            if new['indices'][key] != old['indices'].get(key):
                additions.append(blueprint_index(key, new['indices'][key]))
        blocks.append(schema_block(name, additions))
        
    for name in sorted(after):
        blocks.append(schema_block(name, [blueprint_foreign(k, after[name]['foreign'][k])
                                        for k in sorted(after[name]['foreign'])
                                        if (name, k) in affected or
                                        k not in before.get(name, {}).get('foreign', {})]))
        
    return [block for block in blocks if block]


# Render the complete PHP migration file: up() applies before -> after and
# down() applies the reverse delta.
def migration(before, after):
    up = '\n\n'.join(delta(before, after))
    down = '\n\n'.join(delta(after, before))
    
    db_import = 'use Illuminate\\Support\\Facades\\DB;\n' if 'DB::raw(' in up + down else ''
    return '''<?php

use Illuminate\\Database\\Migrations\\Migration;
use Illuminate\\Database\\Schema\\Blueprint;
use Illuminate\\Support\\Facades\\Schema;
%s
// Generated for Laravel 11+ / MySQL. Review before executing.
// down() restores structure, not deleted data. Renames are drop/add operations.
return new class extends Migration
{
    public function up(): void
    {
%s
    }

    public function down(): void
    {
%s
    }
};
''' % (db_import, up, down)


# Write JSON atomically: write to a temporary file in the same folder, then
# replace the target, so a failure never leaves a half-written history.
def write_json(path, value):
    fd, temp = tempfile.mkstemp(prefix='workbench_', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


# Compare the current model with the saved history, write a new migration file
# into `path` and update the history. With baseline=True, only record the
# current model. Returns the message shown to the user.
def export(path, current, baseline=False):
    history_path = os.path.join(path, HISTORY)
    
    if os.path.exists(history_path):
        with open(history_path, encoding='utf-8') as stream:
            history = json.load(stream)
        if history.get('format') != FORMAT or history.get('schema') != current['schema']:
            raise ValueError('Snapshot version/schema differs from the selected model.')
        
        for entry in history.get('exports', []):
            if not os.path.isfile(os.path.join(path, entry)):
                raise ValueError('Missing previously exported migration: ' + entry)
        if baseline:
            raise ValueError('A baseline already exists. It will not be overwritten.')
    else:
        
        if baseline:
            write_json(history_path, dict(current, exports=[]))
            return 'Baseline saved. No migration was created.'
        if glob.glob(os.path.join(path, '*.php')):
            raise ValueError('Existing migrations without history. Restore the model matching '
                             'the current database and use Save Current Model as Baseline first.')
        history = {'tables': {}, 'exports': []}
        
    before, after = history['tables'], current['tables']
    if before == after:
        return 'No model changes. No migration was created.'
    
    filenames = [os.path.basename(p) for p in glob.glob(os.path.join(path, '*.php'))]
    dates = [datetime.datetime.strptime(n[:17], '%Y_%m_%d_%H%M%S')
             for n in filenames if re.match(r'^\d{4}_\d{2}_\d{2}_\d{6}_', n)]
    date = datetime.datetime.now().replace(microsecond=0)
    if dates:
        date = max(date, max(dates) + datetime.timedelta(seconds=1))
        
    name = date.strftime('%Y_%m_%d_%H%M%S') + ('_create_workbench_schema.php' if not before
                                             else '_update_workbench_schema.php')
    destination = os.path.join(path, name)
    content = migration(before, after)
    
    with open(destination, 'x', encoding='utf-8') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        
    try:
        write_json(history_path, dict(current, exports=history.get('exports', []) + [name]))
    except Exception:
        os.unlink(destination)
        raise
    return 'Created ' + name + '. Review it, then run php artisan migrate.'


# Register the plugin module with Workbench.
ModuleInfo = DefineModule(name='GenerateLaravel5Migration', author='Brandon Eckenrode / revised', version='1.2')


# Plugin entry point (Catalog menu): snapshot the model and open the wizard.
# Any error is shown in a dialog instead of propagating to Workbench.
@ModuleInfo.plugin('wb.util.generate_laravel5_migration', caption='Export Laravel Migration',
                  input=[wbinputs.currentCatalog()], groups=['Catalog/Utilities', 'Menu/Catalog'],
                  pluginMenu='Catalog')
@ModuleInfo.export(grt.INT, grt.classes.db_Catalog)
def generate_laravel5_migration(catalog):
    try:
        current = snapshot(catalog)
        GenerateLaravel5MigrationWizard(current).run()
        return 0
    except Exception as error:
        mforms.Utilities.show_error('Export Laravel Migration', str(error), 'OK', '', '')
        return 1


# Wizard page that previews the migration and offers the export and baseline
# actions.
class GenerateLaravel5MigrationWizardPreviewPage(WizardPage):
    
    def __init__(self, owner, current):
        WizardPage.__init__(self, owner, 'Export migration or baseline')
        self.current = current
        
        self.save_button = mforms.newButton()
        self.save_button.set_text('Generate Migration to Folder...')
        self.save_button.add_clicked_callback(self.save_clicked)
        
        self.baseline_button = mforms.newButton()
        self.baseline_button.set_text('Save Current Model as Baseline...')
        self.baseline_button.add_clicked_callback(self.baseline_clicked)
        
        self.editor = mforms.newCodeEditor()
        self.editor.set_language(mforms.LanguageMySQL)
        self.editor.set_text(migration({}, current['tables']))

    def create_ui(self):
        box = mforms.newBox(True)
        box.set_spacing(12)
        box.add(self.save_button, False, True)
        box.add(self.baseline_button, False, True)
        self.content.add_end(box, False, True)
        self.content.add_end(self.editor, True, True)
        self.content.set_padding(12)
        self.content.set_spacing(12)

    def go_cancel(self):
        self.main.finish()

    def save_clicked(self):
        self.save(False)

    def baseline_clicked(self):
        self.save(True)

    # Ask for the migrations folder, then export a migration or save the
    # baseline, reporting the result (or the error) in a dialog.
    def save(self, baseline):
        
        chooser =mforms.newFileChooser(self.main, mforms.OpenDirectory)
        if chooser.run_modal() != mforms.ResultOk:
            return
        try:
            
            if baseline:
                answer =mforms.Utilities.show_warning('Register existing schema',
                    'The current model must match the existing database structure exactly. '
                    'This records it without executing or creating migrations. Continue?',
                    'Save Baseline', 'Cancel', '')
                if answer != mforms.ResultOk:
                    return
            message = export(chooser.get_path(), self.current, baseline)
            mforms.Utilities.show_message('Laravel migrations', message, 'OK', '', '')
        except Exception as error:
            mforms.Utilities.show_error('Export failed', str(error), 'OK', '', '')


# Single-page wizard window that hosts the preview page.
class GenerateLaravel5MigrationWizard(WizardForm):
    def __init__(self, current):
        WizardForm.__init__(self, None)
        self.set_name('generate_laravel_5_migration_wizard')
        self.set_title('Export Laravel Migration')
        self.add_page(GenerateLaravel5MigrationWizardPreviewPage(self, current))

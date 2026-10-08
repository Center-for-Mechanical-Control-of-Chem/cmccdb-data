import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

// NODE_PATH can point at a separately installed public package backend.
const require = createRequire(import.meta.url);
const {Workbook, SpreadsheetFile} = await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const [input, output] = process.argv.slice(2);
const plan = JSON.parse(await fs.readFile(input, 'utf8'));
const workbook = Workbook.create();
const diagnostics = {backend:'ArtifactTool',literalOnly:true,sheets:[],stage:'creating'};
const checkpoint = async stage => {
  diagnostics.stage=stage;
  diagnostics.memory=process.memoryUsage();
  await fs.writeFile(output+'.backend.json',JSON.stringify(diagnostics));
};
const columnName = n => {
  let name = '';
  while(n) {n--; name = String.fromCharCode(65+n%26)+name; n=Math.floor(n/26);}
  return name;
};
for (const spec of plan.sheets) {
  const sheet = workbook.worksheets.add(spec.name);
  sheet.showGridLines = false;
  const width = Math.max(...spec.rows.map(r=>r.length));
  const rows = spec.rows.map(r=>Array.from({length:width},(_,i)=>{
    const value = r[i] ?? '';
    if (typeof value === 'string' && value.length > 32767) throw new Error('Excel cell text limit exceeded');
    return typeof value === 'string' && value.startsWith('=') ? "'"+value : value;
  }));
  const area = sheet.getRange(`A1:${columnName(width)}${rows.length}`);
  area.format.font = {name:'Arial', size:10, color:'#20242B'};
  area.format.rowHeight = 22;
  area.format.verticalAlignment = 'center';
  // Most evidence cells contain literal strings. A shared range style avoids
  // creating hundreds of thousands of separate per-string formatting mutations.
  // Restore General only for actual numbers/booleans; numeric-looking strings,
  // dates and source labels remain text and must survive exact protobuf re-import.
  area.format.numberFormat = '@';
  for(let row=0;row<rows.length;row++) for(let col=0;col<width;col++) {
    if(typeof rows[row][col]!=='string' && rows[row][col] !== '') {
      sheet.getCell(row,col).format.numberFormat = 'General';
    }
  }
  // Bound the temporary range assignment buffers without dropping blank slots
  // or changing the scientific/header column positions.
  for(let start=0;start<rows.length;start+=256) {
    const chunk=rows.slice(start,start+256);
    sheet.getRange(`A${start+1}:${columnName(width)}${start+chunk.length}`).values=chunk;
  }
  area.format.wrapText = true;
  for (let col=1; col<=width; col++) {
    sheet.getRange(`${columnName(col)}1:${columnName(col)}${rows.length}`).format.columnWidth = spec.widths?.[col-1] ?? 20;
  }
  for(const row of spec.header_rows ?? []) {
    sheet.getRange(`A${row}:${columnName(width)}${row}`).format = {
      fill:'#E7EDF3', font:{name:'Arial',size:10,bold:true,color:'#20242B'}, rowHeight:25
    };
  }
  if(spec.name !== 'ReactionData') {
    area.format.wrapText = true;
    area.format.rowHeight = 48;
    sheet.getRange(`A1:${columnName(width)}1`).format.rowHeight = 30;
    if(spec.name !== 'Figures') {
      // Keep rows usable even when distant evidence/procedure columns contain
      // lengthy text. The complete value remains editable in the formula bar.
      area.format.rowHeight = spec.name === 'Evidence' ? 72 : 60;
      sheet.getRange(`A1:${columnName(width)}1`).format.rowHeight = 36;
    }
    else {
      area.format.rowHeight = 22;
      for(let row=1;row<rows.length;row++) if(rows[row][2] !== '') {
        sheet.getRange(`A${row+1}:${columnName(width)}${row+1}`).format.rowHeight = 44;
      }
    }
  }
  else {
    area.format.rowHeight = 54;
    for(const row of spec.header_rows ?? []) {
      sheet.getRange(`A${row}:${columnName(width)}${row}`).format.rowHeight = 42;
    }
  }
  // Fit wrapped content without shrinking scientific values. Excel's maximum
  // row height is 409 points; the complete longer source text also remains in
  // the cell and lossless audit. Keep header and image rows at their designed heights.
  if(spec.name !== 'Figures') for(let row=0;row<rows.length;row++) {
    if(row===0 || (spec.header_rows ?? []).includes(row+1)) continue;
    const lines = Math.max(1,...rows[row].map((value,col)=>{
      const capacity=Math.max(8,Math.floor((spec.widths?.[col] ?? 20)*0.8));
      return String(value ?? '').split('\n').reduce((total,line)=>total+Math.max(1,Math.ceil(line.length/capacity)),0);
    }));
    const minimum=spec.name==='Evidence' ? 72 : spec.name==='ReactionData' ? 54 : 60;
    sheet.getRange(`A${row+1}:${columnName(width)}${row+1}`).format.rowHeight=Math.min(409,Math.max(minimum,lines*14+12));
    sheet.getRange(`A${row+1}:${columnName(width)}${row+1}`).format.verticalAlignment='top';
  }
  if(spec.freeze_rows) sheet.freezePanes.freezeRows(spec.freeze_rows);
  sheet.freezePanes.freezeColumns(spec.name==='ReactionData' ? 1 : 2);
  for(const note of spec.notes ?? []) {
    workbook.notes.add({id:`${spec.name}:${note.cell}`,target:{cell:{sheetName:spec.name,sheetId:sheet.sheetId,address:note.cell}},
      authorId:'',createdAt:'',body:{plainText:note.text}});
  }
  for(const image of spec.images ?? []) sheet.images.add(image);
  diagnostics.sheets.push({name:spec.name,rows:rows.length,columns:width,
    nonblankCells:rows.reduce((n,row)=>n+row.filter(value=>value!=='').length,0),
    notes:(spec.notes ?? []).length,images:(spec.images ?? []).length});
}
// No formulas are authored: '=' source labels are explicitly escaped literals.
// Recalculate once before final visual checks, inspection and export.
await checkpoint('created');
workbook.recalculate();
diagnostics.recalculated = true;
if(process.env.CMCCDB_RENDER_REVIEW === '1') {
  for(const spec of plan.sheets) {
    const preview = await workbook.render({sheetName:spec.name,
      range:`A1:${columnName(Math.min(spec.rows[0].length,8))}${Math.min(spec.rows.length,25)}`,
      scale:1,format:'png'});
    await fs.writeFile(output+'.'+spec.name+'.preview.png',new Uint8Array(await preview.arrayBuffer()));
  }
}
const inspection = await workbook.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#SPILL!|#CALC!',
  options:{useRegex:true,maxResults:30},maxChars:2000});
await fs.writeFile(output+'.inspect.ndjson',inspection.ndjson);
await checkpoint('exporting');
await (await SpreadsheetFile.exportXlsx(workbook)).save(output);
await checkpoint('complete');

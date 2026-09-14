/**
 * new_doc.js — Chinese Academic Paper Template (课程论文模板)
 *
 * All formatting is pre-configured to GB/T 7714 + Chinese university standards:
 *   - A4 page, 2.5 cm margins on all sides
 *   - SimSun 12pt body, SimHei headings (Cambria Math for English/numbers)
 *   - Manual multi-level heading numbering (synchronized counters)
 *   - Three-line table helper with proper border handling
 *   - LaTeX formula support (block and inline) via temml + Word native math
 *   - Citation superscript handling [n] format
 *   - Reference list [1][2][3] numbering
 *   - Footer: centered page number
 *
 * Usage:
 *   1. Edit the CONTENT SECTION below
 *   2. node scripts/new_doc.js
 *   3. Outputs output.docx (or set OUTPUT_PATH)
 *
 * Dependencies:
 *   npm install docx temml fast-xml-parser
 */

'use strict';

const fs   = require('fs');
const path = require('path');

const {
  Document, Packer,
  Paragraph, TextRun, Math, MathRun,
  Table, TableRow, TableCell,
  Header, Footer,
  PageNumber, AlignmentType, LineRuleType, HeadingLevel,
  LevelFormat, BorderStyle, WidthType, ShadingType, VerticalAlign,
  TableOfContents, PageBreak, ImageRun,
} = require('docx');

// MathML to docx Math converter
const { mathmlToDocxChildren } = require('./mathml-to-docx');
const temml = require('temml');

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const INPUT_MARKDOWN = process.argv[2] ? path.resolve(process.argv[2]) : null;
const OUTPUT_PATH = process.argv[3]
  ? path.resolve(process.argv[3])
  : (INPUT_MARKDOWN
      ? path.join(path.dirname(INPUT_MARKDOWN), `${path.parse(INPUT_MARKDOWN).name}.docx`)
      : 'output.docx');

// Page / margin (DXA: 1440 = 1 inch, 567 ≈ 1 cm)
const PAGE_W        = 11906;   // A4
const PAGE_H        = 16838;
const MARGIN        = 1418;    // 2.5 cm
const CONTENT_W     = PAGE_W - 2 * MARGIN;  // 9070 DXA

// Three-line table border presets
// ISSUE 1 FIX: Use NONE with proper color for invisible borders
const THICK = { style: BorderStyle.SINGLE, size: 12, color: '000000' }; // 1.5 pt
const THIN  = { style: BorderStyle.SINGLE, size: 6,  color: '000000' }; // 0.75 pt
const NONE  = { style: BorderStyle.NONE,   size: 0,  color: 'FFFFFF' };

// ISSUE 3 FIX: Manual heading counters for synchronized numbering
let currentChapter = 0;
let currentSection = 0;
let currentSubsection = 0;

// ─────────────────────────────────────────────────────────────────────────────
// ISSUE 5/7 FIX: Inline Math Detection and Conversion
// ─────────────────────────────────────────────────────────────────────────────

// Unicode math symbols to LaTeX mapping
const UNICODE_TO_LATEX = {
  // Greek letters
  'α': '\\alpha', 'β': '\\beta', 'γ': '\\gamma', 'δ': '\\delta', 'ε': '\\varepsilon',
  'ζ': '\\zeta', 'η': '\\eta', 'θ': '\\theta', 'ι': '\\iota', 'κ': '\\kappa',
  'λ': '\\lambda', 'μ': '\\mu', 'ν': '\\nu', 'ξ': '\\xi', 'π': '\\pi',
  'ρ': '\\rho', 'σ': '\\sigma', 'τ': '\\tau', 'υ': '\\upsilon', 'φ': '\\phi',
  'χ': '\\chi', 'ψ': '\\psi', 'ω': '\\omega',
  'Γ': '\\Gamma', 'Δ': '\\Delta', 'Θ': '\\Theta', 'Λ': '\\Lambda', 'Ξ': '\\Xi',
  'Π': '\\Pi', 'Σ': '\\Sigma', 'Φ': '\\Phi', 'Ψ': '\\Psi', 'Ω': '\\Omega',
  // Subscript digits
  '₀': '_0', '₁': '_1', '₂': '_2', '₃': '_3', '₄': '_4',
  '₅': '_5', '₆': '_6', '₇': '_7', '₈': '_8', '₉': '_9',
  'ₙ': '_n', 'ₓ': '_x', 'ᵢ': '_i', 'ₜ': '_t', 'ₛ': '_s',
  // Superscript
  '⁰': '^0', '¹': '^1', '²': '^2', '³': '^3', '⁴': '^4',
  '⁵': '^5', '⁶': '^6', '⁷': '^7', '⁸': '^8', '⁹': '^9',
  'ⁿ': '^n', 'ⁱ': '^i',
  // Special symbols
  '∞': '\\infty', '∑': '\\sum', '∏': '\\prod', '∫': '\\int',
  '≤': '\\leq', '≥': '\\geq', '≠': '\\neq', '≈': '\\approx',
  '→': '\\to', '←': '\\leftarrow', '↔': '\\leftrightarrow',
  '∈': '\\in', '∉': '\\notin', '⊂': '\\subset', '⊃': '\\supset',
  '∀': '\\forall', '∃': '\\exists', '∧': '\\land', '∨': '\\lor',
  '×': '\\times', '÷': '\\div', '±': '\\pm', '∓': '\\mp',
  '·': '\\cdot', '…': '\\ldots', '⋯': '\\cdots',
  '′': "'", '″': "''",
  '⟨': '\\langle', '⟩': '\\rangle',
  // Superscript letter (for π*, Q*, etc.)
  '*': '^*',
};

/**
 * Convert text with Unicode math symbols to LaTeX format
 * @param {string} text - Text with Unicode math symbols
 * @returns {string} LaTeX formatted text
 */
function unicodeToLatex(text) {
  let result = text;

  // 上标星号（`*`）必须特殊处理：
  // - PDF 转换常见裸星号 `Q*` / `π*`，期望渲染成上标星号 → `Q^*`
  // - 但也常见**已经写成上标**的 `{Q}^{ * }`；若再把其中的 `*` 提升为 `^*`，
  //   会得到 `{Q}^{ ^* }` 这种非法嵌套 —— temml 能渲染但 OMML 转换结果为空，
  //   公式会整条丢失（见 tests/ 回归用例）。
  // 故：先把「已在上标花括号内」的星号规范化为 `^{*}`，再只提升「裸星号」。
  result = result.replace(/\^\s*\{\s*\*\s*\}/g, '^{*}');
  result = result.replace(/(?<![\\^{])\*/g, '^*');

  for (const [unicode, latex] of Object.entries(UNICODE_TO_LATEX)) {
    if (unicode === '*') continue; // 已在上面处理
    result = result.split(unicode).join(latex);
  }
  return result;
}


/**
 * 内联 token 正则（分组顺序：引用 [n] → 行内公式 $...$ → 既有数学启发式）
 *
 * 关键：必须「先切数学与引用」，emphasis 只在切分后的纯文本片段上解析。
 * 否则会把 $...$ 内部的 _{...}（如 {x}_{n}、{Q}^{ * }）从中间撕开。
 */
const INLINE_MATH_SRC =
  '\\$([^$]+)\\$' +
  '|([A-Z][₀₁₂₃₄₅₆₇₈₉ₙₓᵢₜₛ⁰¹²³⁴⁵⁶⁷⁸⁹ⁿⁱ]+\\*?\\s*\\([^)]+\\))' +
  '|([A-Z]\\s*\\([^)]*[αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ₀₁₂₃₄₅₆₇₈₉ₙₓᵢₜₛ][^)]*\\))' +
  '|([αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ][₀₁₂₃₄₅₆₇₈₉ₙₓᵢₜₛ⁰¹²³⁴⁵⁶⁷⁸⁹ⁿⁱ]*\\*?)' +
  '|([A-Za-z][₀₁₂₃₄₅₆₇₈₉ₙₓᵢₜₛ⁰¹²³⁴⁵⁶⁷⁸⁹ⁿⁱ]+\\*?)' +
  '|([A-Z]\\*)';

const INLINE_CITATION_SRC = '(\\[\\d+\\])|';

/**
 * Markdown emphasis：***粗斜体*** / **粗体** / __粗体__ / *斜体* / _斜体_
 * - 开标记后必须非空白、闭标记前必须非空白 → 不会把 "a * b * c" 误判成斜体
 * - 内部至少 1 个字符 → 孤立的 `**` / `*` 会按字面保留，不会被吞掉
 * - 未闭合的标记不匹配，按字面保留（不抛错、不吞字）
 */
const EMPHASIS_RE = /(\*\*\*|___|\*\*|__|\*|_)(?=\S)([\s\S]+?)(?<=\S)\1/;

/** 行内公式未能转成 Word 公式时收集告警（生成结束后统一汇报，避免静默交付） */
const INLINE_MATH_WARNINGS = [];

/**
 * 把纯文本片段按 Markdown emphasis 拆成多个 TextRun（支持嵌套，如 **_粗斜体_**）。
 * @param {Array} children 输出数组
 * @param {string} text 纯文本片段（其中不应包含 $...$ 数学）
 * @param {Object} base 继承样式（如 { bold: true }）
 */
function pushEmphasisRuns(children, text, base = {}) {
  const re = new RegExp(EMPHASIS_RE.source, 'g');
  let last = 0; // 已输出到的位置（其间文本累积为字面 TextRun）
  let m;

  while ((m = re.exec(text)) !== null) {
    const marker = m[1];
    const inner = m[2];
    // 词内下划线（如 foo_bar_baz、{x}_n）不当作斜体：CommonMark 规则。
    // 注意：这里只是「跳过这个起点继续向右找」，不能把文本切断输出，
    // 否则会把 foo_bar_baz 碎成多个 TextRun。
    const prev = m.index > 0 ? text[m.index - 1] : '';
    if (marker[0] === '_' && /\w/.test(prev)) {
      re.lastIndex = m.index + 1;
      continue;
    }

    if (m.index > last) {
      children.push(new TextRun({ ...base, text: text.slice(last, m.index) }));
    }
    const isBold = marker === '***' || marker === '___' || marker === '**' || marker === '__';
    const isItalic = marker === '***' || marker === '___' || marker === '*' || marker === '_';
    pushEmphasisRuns(children, inner, {
      ...base,
      bold: base.bold || isBold || undefined,
      italics: base.italics || isItalic || undefined,
    });

    last = m.index + m[0].length;
    re.lastIndex = last;
  }

  if (last < text.length) {
    children.push(new TextRun({ ...base, text: text.slice(last) }));
  }
  if (children.length === 0 && text) {
    children.push(new TextRun({ ...base, text }));
  }
}

/**
 * 统一内联解析：一次扫描产出【数学公式 / 引用上标 / 粗体 / 斜体】。
 * 所有需要呈现正文文本的地方都应走这里，避免「某条路径忘了解析」。
 * @param {string} text
 * @param {{citations?: boolean, forceBold?: boolean}} options
 *        citations: [n] 是否转上标（默认 true）
 *        forceBold: 整个片段是否强制加粗（表头行用）
 * @returns {Array} TextRun / Math 数组
 */
function buildInlineRuns(text, options = {}) {
  const withCitations = options.citations !== false;
  const base = options.forceBold ? { bold: true } : {};
  const children = [];
  const pattern = new RegExp(
    withCitations ? INLINE_CITATION_SRC + INLINE_MATH_SRC : INLINE_MATH_SRC,
    'g',
  );
  const mathGroupsOf = (match) => (withCitations ? match.slice(2) : match.slice(1));

  let lastIndex = 0;
  let match;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      pushEmphasisRuns(children, text.slice(lastIndex, match.index), base);
    }

    if (withCitations && match[1]) {
      // 引用 [n] → 上标
      children.push(new TextRun({ ...base, text: match[1], superScript: true }));
    } else {
      const mathContent = mathGroupsOf(match).find((v) => v !== undefined);
      if (mathContent) {
        const latex = unicodeToLatex(mathContent);
        let mathChildren = null;
        try {
          const mathml = temml.renderToString(latex, { displayMode: false, throwOnError: false });
          mathChildren = mathmlToDocxChildren(mathml);
        } catch (e) {
          mathChildren = null;
        }
        if (mathChildren && mathChildren.length) {
          children.push(new Math({ children: mathChildren }));
        } else {
          // 行内公式解析失败：保留原文并登记告警
          // （块公式走 latexToMath 会「响亮失败」；行内由告警 + scripts/check_formulas.py 兜底）
          INLINE_MATH_WARNINGS.push(mathContent);
          children.push(new Math({ children: [new MathRun(mathContent)] }));
        }
      }
    }

    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    pushEmphasisRuns(children, text.slice(lastIndex), base);
  }
  if (children.length === 0) {
    pushEmphasisRuns(children, text, base);
  }

  return children;
}

/**
 * 内联解析（不把 [n] 转上标）—— 表格单元格等场景，保持历史行为。
 * @param {string} text
 * @returns {Array} TextRun / Math 数组
 */
function parseInlineContent(text, options = {}) {
  return buildInlineRuns(text, { ...options, citations: false });
}

/**
 * 内联解析（引用 [n] → 上标）—— 正文 / 列表 / 关键词等场景。
 * @param {string} text
 * @returns {Array} TextRun / Math 数组
 */
function parseInlineContentWithCitations(text, options = {}) {
  return buildInlineRuns(text, { ...options, citations: true });
}

// ─────────────────────────────────────────────────────────────────────────────
// Helper: body paragraph (首行缩进 2 字符, 单倍行距) - supports inline formulas
// ─────────────────────────────────────────────────────────────────────────────

function makeBodyParagraph(text, overrides = {}) {
  // 始终走统一内联解析（数学 / 引用 / 粗体 / 斜体）。
  // 旧实现在「无数学且无引用」时短路成 [new TextRun(text)]，
  // 导致正文里的 **粗体** 原样输出到 docx。
  return new Paragraph({
    ...overrides,
    children: parseInlineContentWithCitations(text),
  });
}

function body(text) {
  return makeBodyParagraph(text);
}

function bodyIndented(text, left = 0) {
  return makeBodyParagraph(text, {
    indent: { left, firstLine: 0 },
  });
}

function bodyNoIndent(text, overrides = {}) {
  return makeBodyParagraph(text, {
    indent: { firstLine: 0 },
    ...overrides,
  });
}

// Paragraph with multiple TextRuns
function bodyMulti(runs) {
  return new Paragraph({
    children: runs,
  });
}

const INLINE_LATEX_TO_UNICODE = {
  '\\alpha': 'α',
  '\\beta': 'β',
  '\\gamma': 'γ',
  '\\delta': 'δ',
  '\\epsilon': 'ε',
  '\\varepsilon': 'ε',
  '\\zeta': 'ζ',
  '\\eta': 'η',
  '\\theta': 'θ',
  '\\vartheta': 'ϑ',
  '\\iota': 'ι',
  '\\kappa': 'κ',
  '\\lambda': 'λ',
  '\\mu': 'μ',
  '\\nu': 'ν',
  '\\xi': 'ξ',
  '\\pi': 'π',
  '\\rho': 'ρ',
  '\\sigma': 'σ',
  '\\tau': 'τ',
  '\\upsilon': 'υ',
  '\\phi': 'φ',
  '\\varphi': 'φ',
  '\\chi': 'χ',
  '\\psi': 'ψ',
  '\\omega': 'ω',
  '\\Gamma': 'Γ',
  '\\Delta': 'Δ',
  '\\Theta': 'Θ',
  '\\Lambda': 'Λ',
  '\\Xi': 'Ξ',
  '\\Pi': 'Π',
  '\\Sigma': 'Σ',
  '\\Phi': 'Φ',
  '\\Psi': 'Ψ',
  '\\Omega': 'Ω',
  '\\langle': '⟨',
  '\\rangle': '⟩',
  '\\leq': '≤',
  '\\geq': '≥',
  '\\neq': '≠',
  '\\to': '→',
  '\\rightarrow': '→',
  '\\leftarrow': '←',
  '\\cdot': '·',
  '\\times': '×',
  '\\infty': '∞',
};

const SUBSCRIPT_CHARS = {
  '0': '₀',
  '1': '₁',
  '2': '₂',
  '3': '₃',
  '4': '₄',
  '5': '₅',
  '6': '₆',
  '7': '₇',
  '8': '₈',
  '9': '₉',
  '+': '₊',
  '-': '₋',
  '=': '₌',
  '(': '₍',
  ')': '₎',
  'a': 'ₐ',
  'e': 'ₑ',
  'h': 'ₕ',
  'i': 'ᵢ',
  'j': 'ⱼ',
  'k': 'ₖ',
  'l': 'ₗ',
  'm': 'ₘ',
  'n': 'ₙ',
  'o': 'ₒ',
  'p': 'ₚ',
  'r': 'ᵣ',
  's': 'ₛ',
  't': 'ₜ',
  'u': 'ᵤ',
  'v': 'ᵥ',
  'x': 'ₓ',
  'β': 'ᵦ',
  'γ': 'ᵧ',
  'ρ': 'ᵨ',
  'φ': 'ᵩ',
  'χ': 'ᵪ',
};

const SUPERSCRIPT_CHARS = {
  '0': '⁰',
  '1': '¹',
  '2': '²',
  '3': '³',
  '4': '⁴',
  '5': '⁵',
  '6': '⁶',
  '7': '⁷',
  '8': '⁸',
  '9': '⁹',
  '+': '⁺',
  '-': '⁻',
  '=': '⁼',
  '(': '⁽',
  ')': '⁾',
  'i': 'ⁱ',
  'n': 'ⁿ',
};

function convertCharsWithMap(text, mapping) {
  return [...text].map((char) => mapping[char] || char).join('');
}

function unwrapLatexTextCommands(text) {
  let result = text;
  let previous = '';

  while (result !== previous) {
    previous = result;
    result = result.replace(
      /\\(?:mathrm|mathbf|mathit|text|operatorname)\{([^{}]*)\}/g,
      '$1'
    );
  }

  return result;
}

function simplifyInlineLatex(latex) {
  let text = latex.trim();
  text = unwrapLatexTextCommands(text);

  for (const [command, replacement] of Object.entries(INLINE_LATEX_TO_UNICODE)) {
    text = text.split(command).join(replacement);
  }

  text = text
    .replace(/\\left/g, '')
    .replace(/\\right/g, '')
    .replace(/\\,/g, ' ')
    .replace(/\\;/g, ' ')
    .replace(/\\!/g, '')
    .replace(/\\ /g, ' ')
    .replace(/\\\{/g, '{')
    .replace(/\\\}/g, '}');

  text = text
    .replace(/_\{([^{}]+)\}/g, (_, group) => convertCharsWithMap(group, SUBSCRIPT_CHARS))
    .replace(/_([A-Za-z0-9+\-=()α-ωΑ-Ω])/g, (_, group) => convertCharsWithMap(group, SUBSCRIPT_CHARS))
    .replace(/\^\{([^{}]+)\}/g, (_, group) => group === '*' ? '*' : convertCharsWithMap(group, SUPERSCRIPT_CHARS))
    .replace(/\^([A-Za-z0-9+\-=()α-ωΑ-Ω*])/g, (_, group) => group === '*' ? '*' : convertCharsWithMap(group, SUPERSCRIPT_CHARS));

  text = text
    .replace(/[{}]/g, '')
    .replace(/\s+/g, ' ')
    .trim();

  return text;
}

function buildInlineMathRuns(text, options = {}) {
  const {
    bold = false,
    normalEastAsia = 'SimHei',
    normalItalic = false,
    mathItalic = true,
  } = options;

  const runs = [];
  const pattern = /\$([^$]+)\$/g;
  let lastIndex = 0;
  let match;

  const normalRun = (value) => new TextRun({
    text: value,
    bold,
    italics: normalItalic,
    font: { ascii: 'Cambria Math', eastAsia: normalEastAsia, hAnsi: 'Cambria Math' },
  });

  const mathRun = (value) => new TextRun({
    text: simplifyInlineLatex(value),
    bold,
    italics: mathItalic,
    font: { ascii: 'Cambria Math', eastAsia: normalEastAsia, hAnsi: 'Cambria Math' },
  });

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      runs.push(normalRun(text.slice(lastIndex, match.index)));
    }

    if (match[1].trim()) {
      runs.push(mathRun(match[1]));
    }

    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    runs.push(normalRun(text.slice(lastIndex)));
  }

  if (runs.length === 0) {
    runs.push(normalRun(text));
  }

  return runs;
}

function plainHeading(level, text) {
  return new Paragraph({
    heading: level,
    indent: { firstLine: 0 },
    children: buildInlineMathRuns(text, { bold: true, normalEastAsia: 'SimHei' }),
  });
}

function centeredText(text, options = {}) {
  const {
    size = 24,
    bold = false,
    spacing = { before: 0, after: 120 },
  } = options;

  return new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing,
    indent: { firstLine: 0 },
    children: [new TextRun({
      text,
      bold,
      size,
      font: { ascii: 'Cambria Math', eastAsia: 'SimHei', hAnsi: 'Cambria Math' },
    })],
  });
}

function bullet(text) {
  return new Paragraph({
    numbering: { reference: 'bullets', level: 0 },
    children: buildInlineRuns(text),
  });
}

function parseHtmlTable(html) {
  const rowMatches = [...html.matchAll(/<tr>(.*?)<\/tr>/gsi)];
  const rows = rowMatches.map((rowMatch) => {
    const cellMatches = [...rowMatch[1].matchAll(/<t[dh]>(.*?)<\/t[dh]>/gsi)];
    return cellMatches.map((cellMatch) =>
      cellMatch[1]
        .replace(/<[^>]+>/g, '')
        .replace(/&nbsp;/g, ' ')
        .trim()
    );
  }).filter((row) => row.length);

  if (rows.length < 2) {
    throw new Error('HTML table requires at least one header row and one body row');
  }

  return {
    headers: rows[0],
    rows: rows.slice(1),
  };
}

function normalizeCaptionText(text) {
  return text
    .replace(/^([\u56fe\u8868])(\d)/, '$1 $2')
    .replace(/\s+/g, ' ')
    .trim();
}

function getImageType(imagePath) {
  const ext = path.extname(imagePath).toLowerCase();
  if (ext === '.jpg') return 'jpg';
  if (ext === '.jpeg') return 'jpeg';
  if (ext === '.png') return 'png';
  if (ext === '.gif') return 'gif';
  if (ext === '.bmp') return 'bmp';
  throw new Error(`Unsupported image format: ${imagePath}`);
}

function getImageDimensions(buffer, imagePath) {
  const ext = path.extname(imagePath).toLowerCase();

  if (ext === '.png') {
    return {
      width: buffer.readUInt32BE(16),
      height: buffer.readUInt32BE(20),
    };
  }

  if (ext === '.jpg' || ext === '.jpeg') {
    let offset = 2;
    while (offset < buffer.length) {
      if (buffer[offset] !== 0xFF) {
        offset += 1;
        continue;
      }
      const marker = buffer[offset + 1];
      const length = buffer.readUInt16BE(offset + 2);
      if (marker >= 0xC0 && marker <= 0xC3) {
        return {
          height: buffer.readUInt16BE(offset + 5),
          width: buffer.readUInt16BE(offset + 7),
        };
      }
      offset += 2 + length;
    }
  }

  throw new Error(`Unable to read image dimensions: ${imagePath}`);
}

function imageBlock(imagePath) {
  const data = fs.readFileSync(imagePath);
  const { width, height } = getImageDimensions(data, imagePath);
  const maxWidth = 460;
  const maxHeight = 360;
  const scale = globalThis.Math.min(maxWidth / width, maxHeight / height, 1);

  return new Paragraph({
    alignment: AlignmentType.CENTER,
    indent: { firstLine: 0 },
    spacing: { before: 120, after: 60 },
    children: [new ImageRun({
      type: getImageType(imagePath),
      data,
      transformation: {
        width: globalThis.Math.round(width * scale),
        height: globalThis.Math.round(height * scale),
      },
      altText: {
        title: path.basename(imagePath),
        description: path.basename(imagePath),
        name: path.basename(imagePath),
      },
    })],
  });
}

/**
 * 从原始 LaTeX 中剥离尾部的 \tag{n} 编号。
 * 多行块与单行块公式共用，保证编号处理只有一处实现。
 * @returns {{latex: string, number: string}}
 */
function splitTag(rawLatex) {
  const trimmed = (rawLatex || '').trim();
  const tagMatch = trimmed.match(/\\tag\{([^}]+)\}\s*$/);
  if (!tagMatch) return { latex: trimmed, number: '' };
  return { latex: trimmed.slice(0, tagMatch.index).trim(), number: tagMatch[1] };
}

function parseFormulaBlock(lines, startIndex) {
  const formulaLines = [];
  let i = startIndex + 1;
  while (i < lines.length && lines[i].trim() !== '$$') {
    formulaLines.push(lines[i]);
    i += 1;
  }

  const { latex, number } = splitTag(formulaLines.join(' '));

  return {
    block: formula(latex, number),
    nextIndex: i,
  };
}

function buildContentFromMarkdown(markdownPath) {
  const raw = fs.readFileSync(markdownPath, 'utf8');
  const lines = raw.split(/\r?\n/);
  const content = [];
  const baseDir = path.dirname(markdownPath);
  let i = 0;
  let inReferences = false;
  let abstractInserted = false;
  let generatedToc = false;
  let mainHeadingCount = 0;

  const flushParagraph = (paragraphLines) => {
    if (!paragraphLines.length) return;
    const rawText = paragraphLines.join(' ').replace(/\s+/g, ' ').trim();
    if (!rawText) return;

    const leadingTabs = (paragraphLines[0].match(/^\t+/) || [''])[0].length;
    const leadingSpaces = (paragraphLines[0].match(/^ +/) || [''])[0].length;
    const left = leadingTabs > 0 ? leadingTabs * 420 : (leadingSpaces >= 4 ? 420 : 0);
    content.push(left > 0 ? bodyIndented(rawText, left) : body(rawText));
  };

  while (i < lines.length) {
    const rawLine = lines[i];
    const line = rawLine.trim();

    if (!line) {
      i += 1;
      continue;
    }

    if (line === '## \u76ee\u5f55') {
      i += 1;
      while (i < lines.length && !lines[i].startsWith('# ')) {
        i += 1;
      }
      continue;
    }

    if (line.startsWith('# ')) {
      content.push(centeredText(line.slice(2).trim(), {
        size: 36,
        bold: true,
        spacing: { before: 0, after: 240 },
      }));
      i += 1;
      continue;
    }

    if (/^##\s+.+\(name\)\s*$/.test(line)) {
      content.push(centeredText(line.replace(/^##\s+/, ''), {
        size: 24,
        spacing: { before: 0, after: 180 },
      }));
      i += 1;
      continue;
    }

    if (/^##\s*\u6458\u8981[:\uff1a]?\s*$/.test(line)) {
      content.push(centeredText('\u6458\u8981', { bold: true, spacing: { before: 0, after: 120 } }));
      i += 1;
      abstractInserted = true;
      continue;
    }

    if (/^\u5173\u952e\u8bcd[:\uff1a]/.test(line)) {
      content.push(new Paragraph({
        indent: { firstLine: 0 },
        children: [
          new TextRun({ text: '\u5173\u952e\u8bcd\uff1a', bold: true }),
          ...parseInlineContentWithCitations(line.replace(/^\u5173\u952e\u8bcd[:\uff1a]\s*/, '')),
        ],
      }));
      if (abstractInserted && !generatedToc) {
        content.push(pageBreak());
        content.push(new TableOfContents('\u76ee\u5f55', { hyperlink: true, headingStyleRange: '1-3' }));
        content.push(pageBreak());
        generatedToc = true;
      }
      i += 1;
      continue;
    }

    if (line === '$$') {
      const { block, nextIndex } = parseFormulaBlock(lines, i);
      content.push(block);
      i = nextIndex + 1;
      continue;
    }

    // 单行块公式：$$...$$（可选前置引导文字，如「由…可得 $$E=mc^2 \tag{1}$$」）
    // 约束：
    //  - 放在 line === '$$' 之后 → 与「单独一行 $$」（多行块起止）天然不冲突
    //  - 引导文字与公式内部都不允许再出现 `$$` → 保证「一行内只有一个块公式」
    const singleLineBlock = line.match(
      /^((?:(?!\$\$)[\s\S])*?)\$\$((?:(?!\$\$)[\s\S])+?)\$\$$/,
    );
    if (singleLineBlock) {
      const leading = singleLineBlock[1].trim();
      if (leading) content.push(body(leading));
      const { latex, number } = splitTag(singleLineBlock[2]);
      content.push(formula(latex, number));
      i += 1;
      continue;
    }

    // 含 `$$` 但没被上面的规则接住 = 写法不规范（一行多个公式 / 公式后又跟内容）。
    // 这里必须报错：否则会退化成行内公式，导致正文字面残留 `$`、编号丢失、公式降级。
    if (line.includes('$$')) {
      throw new Error(
        `[formula] 无法解析的块公式写法：\n  ${line}\n` +
        `  规则：一行内只能有一个块公式，且公式后面不要再跟内容。\n` +
        `  请改为每行一个，或使用多行写法：\n` +
        `   $$\n   ...\\tag{n}\n   $$`
      );
    }

    const imageMatch = line.match(/^!\[[^\]]*]\(([^)]+)\)$/);
    if (imageMatch) {
      const imagePath = path.resolve(baseDir, imageMatch[1]);
      content.push(imageBlock(imagePath));
      let j = i + 1;
      while (j < lines.length && !lines[j].trim()) {
        j += 1;
      }
      if (j < lines.length && /^\u56fe/.test(lines[j].trim())) {
        content.push(figCaption(normalizeCaptionText(lines[j].trim())));
        i = j + 1;
      } else {
        i += 1;
      }
      continue;
    }

    if (line.startsWith('<table>')) {
      const { headers, rows } = parseHtmlTable(line);
      if (content.length && i > 0 && /^\u8868/.test(lines[i - 1].trim())) {
        content.pop();
        content.push(tableCaption(normalizeCaptionText(lines[i - 1].trim())));
      }
      const columnWidths = headers.length === 2
        ? [1800, CONTENT_W - 1800]
        : Array(headers.length).fill(globalThis.Math.floor(CONTENT_W / headers.length));
      if (headers.length > 2) {
        columnWidths[columnWidths.length - 1] = CONTENT_W - columnWidths.slice(0, -1).reduce((sum, n) => sum + n, 0);
      }
      content.push(threeLineTable(headers, rows, columnWidths));
      i += 1;
      continue;
    }

    if (line === '---') {
      i += 1;
      continue;
    }

    if (/^##\s+\u53c2\u8003\u6587\u732e\s*$/.test(line)) {
      inReferences = true;
      content.push(pageBreak());
      content.push(plainHeading(HeadingLevel.HEADING_1, '\u53c2\u8003\u6587\u732e'));
      i += 1;
      continue;
    }

    if (/^##\s+/.test(line)) {
      const text = line.replace(/^##\s+/, '').trim();
      if (/^[\u4e00-\u5341]+\u3001/.test(text)) {
        if (mainHeadingCount > 0) {
          content.push(pageBreak());
        }
        mainHeadingCount += 1;
      }
      content.push(plainHeading(HeadingLevel.HEADING_1, text));
      inReferences = false;
      i += 1;
      continue;
    }

    if (/^###\s+/.test(line)) {
      content.push(plainHeading(HeadingLevel.HEADING_2, line.replace(/^###\s+/, '').trim()));
      i += 1;
      continue;
    }

    if (/^####\s+/.test(line)) {
      content.push(plainHeading(HeadingLevel.HEADING_3, line.replace(/^####\s+/, '').trim()));
      i += 1;
      continue;
    }

    if (/^\[\d+\]\s+/.test(line) && inReferences) {
      content.push(ref(line.replace(/^\[\d+\]\s+/, '').trim()));
      i += 1;
      continue;
    }

    if (/^- /.test(line)) {
      content.push(bullet(line.replace(/^- /, '').trim()));
      i += 1;
      continue;
    }

    const paragraphLines = [rawLine];
    let j = i + 1;
    while (j < lines.length) {
      const nextLine = lines[j];
      const nextTrimmed = nextLine.trim();
      if (
        !nextTrimmed ||
        nextTrimmed === '$$' ||
        nextTrimmed === '---' ||
        /^#/.test(nextTrimmed) ||
        /^!\[/.test(nextTrimmed) ||
        nextTrimmed.startsWith('<table>') ||
        /^- /.test(nextTrimmed) ||
        (/^\[\d+\]\s+/.test(nextTrimmed) && inReferences)
      ) {
        break;
      }
      paragraphLines.push(nextLine);
      j += 1;
    }
    flushParagraph(paragraphLines);
    i = j;
  }

  return content;
}

// ?????????????????????????????????????????????????????????????????????????????
// ISSUE 3 FIX: Manual heading numbering for synchronized counters
// ?????????????????????????????????????????????????????????????????????????????

/** Reset heading counters (call at start of document) */
function resetHeadingCounters() {
  currentChapter = 0;
  currentSection = 0;
  currentSubsection = 0;
}

/** H1 - Level 1 heading (manual Chinese numbering: 一、二、三) */
function h1Manual(text) {
  currentChapter++;
  currentSection = 0;
  currentSubsection = 0;
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    indent: { firstLine: 0 },
    children: buildInlineMathRuns(text, { bold: true, normalEastAsia: 'SimHei' }),
  });
}

/** H1 with auto-numbering (→ 1  2  3) - use when you want Arabic numerals */
function h1(text) {
  currentChapter++;
  currentSection = 0;
  currentSubsection = 0;
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    indent: { firstLine: 0 },
    children: [
      new TextRun({
        text: `${currentChapter} `,
        bold: true,
        font: { ascii: 'Cambria Math', eastAsia: 'SimHei', hAnsi: 'Cambria Math' },
      }),
      ...buildInlineMathRuns(text, { bold: true, normalEastAsia: 'SimHei' }),
    ],
  });
}

/** H2 - Level 2 heading (manual numbering: chapter.section, e.g., 1.1, 2.3) */
function h2(text) {
  currentSection++;
  currentSubsection = 0;
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    indent: { firstLine: 0 },
    children: [
      new TextRun({
        text: `${currentChapter}.${currentSection} `,
        bold: true,
        font: { ascii: 'Cambria Math', eastAsia: 'SimHei', hAnsi: 'Cambria Math' },
      }),
      ...buildInlineMathRuns(text, { bold: true, normalEastAsia: 'SimHei' }),
    ],
  });
}

/** H3 - Level 3 heading (manual numbering: chapter.section.subsection, e.g., 1.1.1) */
function h3(text) {
  currentSubsection++;
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    indent: { firstLine: 0 },
    children: [
      new TextRun({
        text: `${currentChapter}.${currentSection}.${currentSubsection} `,
        bold: true,
        font: { ascii: 'Cambria Math', eastAsia: 'SimHei', hAnsi: 'Cambria Math' },
      }),
      ...buildInlineMathRuns(text, { bold: true, normalEastAsia: 'SimHei' }),
    ],
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Helper: captions - ISSUE 4 FIX: Mixed fonts for English/numbers
// ─────────────────────────────────────────────────────────────────────────────

/** Figure caption (below figure). label e.g. "图 1-1 系统架构" */
function figCaption(label) {
  return new Paragraph({
    style: 'FigureCaption',
    children: buildInlineMathRuns(label, { bold: true, normalEastAsia: 'SimSun' }),
  });
}

/** Table caption (above table). label e.g. "表 1-1 符号说明" */
function tableCaption(label) {
  return new Paragraph({
    style: 'TableCaption',
    children: buildInlineMathRuns(label, { bold: true, normalEastAsia: 'SimSun' }),
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// ISSUE 1 FIX: 三线表 (three-line table) with proper border handling
// Body row borders must be NONE (not just thin), only:
// - Header top: THICK
// - Header bottom: THIN
// - Last row bottom: THICK
// ─────────────────────────────────────────────────────────────────────────────

function threeLineTable(headers, rows, colWidths) {
  const n = headers.length;

  // Default: equal column widths
  if (!colWidths) {
    const w = globalThis.Math.floor(CONTENT_W / n);
    colWidths = Array(n).fill(w);
    colWidths[n - 1] = CONTENT_W - w * (n - 1);
  }

  if (colWidths.length !== n) throw new Error('colWidths length must match headers length');

  // Cell helper function - supports math content in cells
  const cellOf = (text, w, borders, bold = false) => {
    // 统一内联解析（数学 / 粗体 / 斜体）；表头整体加粗通过 forceBold 传递，
    // 而不是事后重建 TextRun（旧写法会丢掉斜体等 run 级样式）。
    // 这里保持历史行为：单元格内 [n] 不做上标处理。
    const cellChildren = buildInlineRuns(text, { citations: false, forceBold: bold });
    
    return new TableCell({
      width:   { size: w, type: WidthType.DXA },
      borders,
      shading: { fill: 'FFFFFF', type: ShadingType.CLEAR },
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        indent:    { firstLine: 0 },
        children:  cellChildren,
      })],
    });
  };

  // Header row: thick top, thin bottom, no sides
  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) =>
      cellOf(h, colWidths[i], { top: THICK, bottom: THIN, left: NONE, right: NONE }, true)
    ),
  });

  // Body rows: NO borders except last gets thick bottom
  // ISSUE 1 FIX: All body row borders are NONE, only last row bottom is THICK
  const bodyRows = rows.map((row, ri) => {
    const isLast = ri === rows.length - 1;
    return new TableRow({
      children: row.map((cell, i) =>
        cellOf(String(cell), colWidths[i], {
          top:    NONE,
          bottom: isLast ? THICK : NONE,
          left:   NONE,
          right:  NONE,
        })
      ),
    });
  });

  return new Table({
    width:        { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: colWidths,
    rows:         [headerRow, ...bodyRows],
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Helper: reference entry (GB/T 7714-2015)
// ─────────────────────────────────────────────────────────────────────────────

function ref(text) {
  return new Paragraph({
    style: 'Reference',
    numbering: { reference: 'references', level: 0 },
    children: [new TextRun(text)],
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Helper: empty paragraph (spacing)
// ─────────────────────────────────────────────────────────────────────────────

function blank() {
  return new Paragraph({ children: [] });
}

// ─────────────────────────────────────────────────────────────────────────────
// ISSUE 10 FIX: Page break helper
// Insert after abstract/keywords, and before references section
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Create a page break paragraph
 * Use after keywords section and before references section
 * @returns {Paragraph} Paragraph containing a page break
 */
function pageBreak() {
  return new Paragraph({ children: [new PageBreak()] });
}


// ─────────────────────────────────────────────────────────────────────────────
// ISSUE 5 FIX: Block formula using temml + mathmlToDocxChildren (Word native math)
// ISSUE 2 FIX: Formula table borders all set to NONE including insideHorizontal/insideVertical
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Convert LaTeX formula to docx Math component（块公式路径）
 *
 * 失败必须「响亮失败」：绝不静默回退成 MathRun(原始 LaTeX)，
 * 否则用户拿到的 docx 里会直接显示 LaTeX 源码（例如 "F_{c3} = ma \tag{2}"）。
 * @param {string} latex - LaTeX formula string
 * @returns {Math} docx Math object
 */
function latexToMath(latex) {
  let mathml;
  try {
    mathml = temml.renderToString(latex, { displayMode: true, throwOnError: true });
  } catch (e) {
    throw new Error(
      `[formula] LaTeX 解析失败：${latex}\n` +
      `  temml：${String(e.message).split('\n')[0]}\n` +
      `  常见原因：使用了 temml/KaTeX 不支持的宏；或括号、花括号不配对。\n` +
      `  块公式请使用多行写法，并让 \\tag{n} 单独占一行：\n` +
      `   $$\n   ...\\tag{n}\n   $$`
    );
  }

  // 双保险：temml 在 throwOnError 未覆盖的场景下会用「红色错误文本 / 错误块」呈现而不是抛错
  if (mathml.includes('temml-error') || mathml.includes('color:#b22222')) {
    throw new Error(
      `[formula] LaTeX 解析异常（temml 内部报错）：${latex}\n` +
      `  请检查是否使用了不支持的宏，或数学语法有误（如 ^ / _ 后面缺花括号）。`
    );
  }

  const children = mathmlToDocxChildren(mathml);
  if (!children || !children.length) {
    throw new Error(
      `[formula] LaTeX 转换后未生成任何公式内容：${latex}\n` +
      `  请检查公式语法是否正确。`
    );
  }

  return new Math({ children });
}

/**
 * Block formula layout using 3-column borderless table
 * ISSUE 2 FIX: All borders including insideHorizontal/insideVertical set to NONE
 * @param {string} latex - LaTeX formula string
 * @param {number|string} number - Equation number
 * @returns {Table} Formula table
 */
function formula(latex, number) {
  // Use 3-column borderless table layout: left margin | centered formula | right-aligned number
  const noBorders = { top: NONE, bottom: NONE, left: NONE, right: NONE };
  
  const leftCell = new TableCell({
    width: { size: 567, type: WidthType.DXA },
    borders: noBorders,
    shading: { fill: 'FFFFFF', type: ShadingType.CLEAR },
    verticalAlign: VerticalAlign.CENTER,  // ISSUE 11 FIX: Center content vertically
    children: [new Paragraph({ indent: { firstLine: 0 }, children: [] })],
  });
  
  // Use temml + mathmlToDocxChildren to create Word native formula
  const mathObj = latexToMath(latex);
  const formulaCell = new TableCell({
    width: { size: 7936, type: WidthType.DXA },
    borders: noBorders,
    shading: { fill: 'FFFFFF', type: ShadingType.CLEAR },
    verticalAlign: VerticalAlign.CENTER,  // ISSUE 11 FIX: Center content vertically
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      indent: { firstLine: 0 },
      children: [mathObj],
    })],
  });
  
  const numberCell = new TableCell({
    width: { size: 567, type: WidthType.DXA },
    borders: noBorders,
    shading: { fill: 'FFFFFF', type: ShadingType.CLEAR },
    verticalAlign: VerticalAlign.CENTER,  // ISSUE 11 FIX: Center content vertically
    children: [new Paragraph({
      alignment: AlignmentType.RIGHT,
      indent: { firstLine: 0 },
      children: [new TextRun(`(${number})`)],
    })],
  });
  
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: [567, 7936, 567],
    // ISSUE 2 FIX: Include insideHorizontal and insideVertical as NONE
    borders: {
      top: NONE,
      bottom: NONE,
      left: NONE,
      right: NONE,
      insideHorizontal: NONE,
      insideVertical: NONE,
    },
    rows: [new TableRow({ 
      children: [leftCell, formulaCell, numberCell],
    })],
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Document structure: styles + numbering
// ISSUE 4/6 FIX: Mixed fonts for headings/captions (Cambria Math for English/numbers)
// ─────────────────────────────────────────────────────────────────────────────

const STYLES = {
  default: {
    document: {
      // English/math: Cambria Math. Substitute "Times New Roman" if preferred.
      run: {
        font: { ascii: 'Cambria Math', hAnsi: 'Cambria Math', eastAsia: 'SimSun' },
        size: 24,  // 12pt (half-points)
      },
      // Line spacing: single. Alternatives: fixed 20pt → line:400,EXACT | 1.5x → line:360,AUTO
      paragraph: {
        spacing: { line: 240, lineRule: LineRuleType.AUTO },
        indent:  { firstLine: 480 },  // 2-character indent
      },
    },
  },
  paragraphStyles: [
    {
      // ISSUE 4/6 FIX: Heading fonts use Cambria Math for ascii/hAnsi, SimHei for eastAsia
      id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true,
      run: { font: { ascii: 'Cambria Math', eastAsia: 'SimHei', hAnsi: 'Cambria Math' }, size: 32, bold: true },
      paragraph: {
        alignment:    AlignmentType.CENTER,
        indent:       { firstLine: 0 },
        spacing:      { line: 288, lineRule: LineRuleType.AUTO },
        outlineLevel: 0,
      },
    },
    {
      id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true,
      run: { font: { ascii: 'Cambria Math', eastAsia: 'SimHei', hAnsi: 'Cambria Math' }, size: 28, bold: true },
      paragraph: {
        alignment:    AlignmentType.LEFT,
        indent:       { firstLine: 0 },
        spacing:      { before: 120, after: 120, line: 360, lineRule: LineRuleType.AUTO },
        outlineLevel: 1,
      },
    },
    {
      id: 'Heading3', name: 'Heading 3', basedOn: 'Normal', next: 'Normal', quickFormat: true,
      run: { font: { ascii: 'Cambria Math', eastAsia: 'SimHei', hAnsi: 'Cambria Math' }, size: 24, bold: true },
      paragraph: {
        alignment:    AlignmentType.LEFT,
        indent:       { firstLine: 0 },
        spacing:      { before: 120, after: 120, line: 360, lineRule: LineRuleType.AUTO },
        outlineLevel: 2,
      },
    },
    {
      // ISSUE 4 FIX: Figure/Table captions use Cambria Math for English/numbers
      id: 'FigureCaption', name: 'Figure Caption', basedOn: 'Normal',
      run: { font: { ascii: 'Cambria Math', eastAsia: 'SimSun', hAnsi: 'Cambria Math' }, size: 22, bold: true },
      paragraph: {
        alignment: AlignmentType.CENTER,
        indent:    { firstLine: 0 },
        spacing:   { before: 120, after: 60, line: 240, lineRule: LineRuleType.AUTO },
      },
    },
    {
      id: 'TableCaption', name: 'Table Caption', basedOn: 'Normal',
      run: { font: { ascii: 'Cambria Math', eastAsia: 'SimSun', hAnsi: 'Cambria Math' }, size: 22, bold: true },
      paragraph: {
        alignment: AlignmentType.CENTER,
        indent:    { firstLine: 0 },
        spacing:   { before: 120, after: 60, line: 240, lineRule: LineRuleType.AUTO },
      },
    },
    {
      id: 'Reference', name: 'Reference', basedOn: 'Normal',
      run: { font: { ascii: 'Cambria Math', hAnsi: 'Cambria Math', eastAsia: 'SimSun' }, size: 24 },
      paragraph: {
        spacing: { line: 240, lineRule: LineRuleType.AUTO },
        indent:  { left: 480, hanging: 480, firstLine: 0 },
      },
    },
  ],
};

const NUMBERING = {
  config: [
    {
      reference: 'references',
      levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: '[%1]',
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 480, hanging: 480 } } } },
      ],
    },
    {
      reference: 'bullets',
      levels: [
        { level: 0, format: LevelFormat.BULLET, text: '•',
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
      ],
    },
    {
      reference: 'numbers',
      levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: '%1.',
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
      ],
    },
  ],
};

// ─────────────────────────────────────────────────────────────────────────────
// ██  CONTENT SECTION — Edit below this line  ████████████████████████████████
// ─────────────────────────────────────────────────────────────────────────────

// Reset counters before building content
resetHeadingCounters();

const CONTENT = INPUT_MARKDOWN
  ? buildContentFromMarkdown(INPUT_MARKDOWN)
  : [
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing:   { before: 0, after: 240 },
        indent:    { firstLine: 0 },
        children:  [new TextRun({ text: '????', bold: true, size: 36,
                                  font: { ascii: 'Cambria Math', eastAsia: 'SimHei', hAnsi: 'Cambria Math' } })],
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        indent:    { firstLine: 0 },
        children:  [new TextRun({ text: '??', bold: true })],
      }),
      body('???????????Q-learning????????????????'),
      new Paragraph({
        indent:    { firstLine: 0 },
        children:  [
          new TextRun({ text: '\u5173\u952e\u8bcd\uff1a', bold: true }),
          new TextRun('?????Q-learning?????????'),
        ],
      }),
      pageBreak(),
      h1Manual('????'),
      blank(),
      body('?????Reinforcement Learning??????????????'),
      h2('????'),
      blank(),
      body('??????????????????[1][2]'),
      h3('????'),
      blank(),
      body('?????????????????'),
      blank(),
      body('Q-Learning ??????:'),
      blank(),
      formula('Q_n(x, a) = (1 - \\alpha_n) Q_{n-1}(x, a) + \\alpha_n [r_n + \\gamma V_{n-1}(y_n)]', 1),
      blank(),
      blank(),
      tableCaption('? 1-1 ????'),
      threeLineTable(
        ['??', '??'],
        [
          ['S',   '????????????????'],
          ['A',   '?????????????????'],
          ['Q?(x,a)', 'Q?????n??????x???a???'],
        ],
        [1800, 7270]
      ),
      blank(),
      h1Manual('????'),
      blank(),
      body('????????? Q-learning ???'),
      pageBreak(),
      new Paragraph({
        heading: HeadingLevel.HEADING_1,
        indent:  { firstLine: 0 },
        children: [new TextRun('????')],
      }),
      blank(),
      ref('Watkins C J C H, Dayan P. Q-learning[J]. Machine learning, 1992, 8(3): 279-292.'),
      ref('Sutton R S, Barto A G. Reinforcement Learning: An Introduction[M]. 2nd ed. Cambridge: MIT Press, 2018.'),
    ];

// ?????????????????????????????????????????????????????????????????????????????
// Build & write document
// ─────────────────────────────────────────────────────────────────────────────

const doc = new Document({
  styles:    STYLES,
  numbering: NUMBERING,
  sections: [{
    properties: {
      page: {
        size:   { width: PAGE_W, height: PAGE_H },
        margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
      },
    },
    footers: {
      default: new Footer({
        children: [
          new Paragraph({
            alignment: AlignmentType.CENTER,
            indent:    { firstLine: 0 },
            children:  [new TextRun({ children: [PageNumber.CURRENT] })],
          }),
        ],
      }),
    },
    children: CONTENT,
  }],
});

Packer.toBuffer(doc).then(buf => {
  const out = path.resolve(OUTPUT_PATH);
  fs.writeFileSync(out, buf);
  console.log(`✓  Written: ${out}`);

  // 行内公式若有未能转换的，明确汇报（不静默交付）
  if (INLINE_MATH_WARNINGS.length) {
    console.error(
      `\n[formula] 警告：${INLINE_MATH_WARNINGS.length} 处行内公式未能转为 Word 原生公式（已按文本保留）：`,
    );
    for (const w of INLINE_MATH_WARNINGS.slice(0, 10)) console.error(`  - ${w}`);
    console.error('  建议修正后重跑，或运行 scripts/check_formulas.py 复核产物。');
    process.exitCode = 1;
  }
}).catch(err => {
  console.error('Error building document:', err.message);
  process.exit(1);
});

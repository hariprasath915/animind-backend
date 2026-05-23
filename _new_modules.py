# ======================================================================
#  MODULE 7.6 — NEW Quiz Generator v10 (3 sets x 5 questions)
#  Light-theme educational quiz. Replaces old dark-theme quiz system.
# ======================================================================

QUIZ_SYSTEM_PROMPT_V10 = """You are QAnim Quiz Engine v10. Generate quiz questions as a JSON array.

Return ONLY a valid JSON array of 15 question objects. No markdown fences.

Each question: {"question":"...","type":"MCQ|True/False|Numerical|Reasoning",
"options":["A","B","C","D"],"correct":0,"explanation":"..."}

Distribution: Set1(Q1-5 conceptual), Set2(Q6-10 application), Set3(Q11-15 advanced).
Questions must relate to the topic. Vary difficulty across sets."""

QUIZ_PROMPT_V10 = """Generate 15 quiz questions for this topic.

QUESTION (context): {question}
CATEGORY: {category}

Return ONLY a JSON array of 15 objects. No other text."""


class QuizGenerator:
    """v10 Quiz Generator - 3 sets x 5 questions = 15 total, light theme."""

    @classmethod
    async def generate(cls, question: str, category: str) -> str:
        QAnimLogger.info("QuizGen", "Generating v10 quiz for category=" + category)
        prompt = QUIZ_PROMPT_V10.format(question=question[:400], category=category)

        quiz_questions = None
        try:
            msg = client.messages.create(
                model=QUIZ_MODEL, max_tokens=MAX_TOK_QUIZ,
                system=QUIZ_SYSTEM_PROMPT_V10,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = msg.content[0].text.strip()
            QAnimLogger.info("QuizGen", "model=" + QUIZ_MODEL + " len=" + str(len(raw)))
            quiz_questions = cls._extract_questions(raw)
        except Exception as e:
            QAnimLogger.error("QuizGen", "API error: " + str(e))

        if not quiz_questions or len(quiz_questions) < 5:
            QAnimLogger.warn("QuizGen", "Using fallback questions")
            quiz_questions = cls._fallback_questions(category)

        while len(quiz_questions) < 15:
            quiz_questions.extend(cls._fallback_questions(category))
        quiz_questions = quiz_questions[:15]

        quiz_html = cls._build_quiz_html(quiz_questions, question, category)
        QAnimLogger.ok("QuizGen", "Quiz generated (" + str(len(quiz_html)) + " chars)")
        return quiz_html

    @classmethod
    def _extract_questions(cls, raw: str) -> list:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
        except Exception:
            pass
        m = re.search(r'\[.+\]', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return []

    @classmethod
    def _fallback_questions(cls, category: str) -> list:
        return [
            {"question": "What is the fundamental principle governing this type of problem?",
             "type": "MCQ", "options": ["Conservation law or core equation", "Random chance",
             "External force only", "No governing principle"], "correct": 0,
             "explanation": "Every system is governed by fundamental principles or equations."},
            {"question": "True or False: All variables in this system are independent.",
             "type": "True/False", "options": ["True", "False"], "correct": 1,
             "explanation": "Variables are related through the governing equation."},
            {"question": "Why is identifying given quantities important before solving?",
             "type": "MCQ", "options": ["To impress others", "To choose the correct formula",
             "It is not important", "Only for exams"], "correct": 1,
             "explanation": "Identifying given quantities ensures correct formula selection."},
            {"question": "What happens when you change a key variable in this system?",
             "type": "Reasoning", "options": ["Nothing changes", "Other quantities adjust",
             "The system breaks", "Only unrelated quantities change"], "correct": 1,
             "explanation": "In interconnected systems, changing one variable affects others."},
            {"question": "Which approach is best for solving this type of problem?",
             "type": "MCQ", "options": ["Guess and check", "Systematic step-by-step analysis",
             "Skip to the answer", "Use approximation only"], "correct": 1,
             "explanation": "A systematic approach ensures accuracy and understanding."},
        ]

    @classmethod
    def _build_quiz_html(cls, questions: list, question: str, category: str) -> str:
        q_safe = html_module.escape(question[:100])
        cat_safe = html_module.escape(category)

        sets_html = ""
        set_names = ["Set 1 : Conceptual", "Set 2 : Application", "Set 3 : Advanced"]
        for set_idx in range(3):
            start_i = set_idx * 5
            end_i = start_i + 5
            set_qs = questions[start_i:end_i]
            cards = ""
            for qi, q in enumerate(set_qs):
                q_num = start_i + qi + 1
                q_text = html_module.escape(str(q.get("question", "")))
                q_type = html_module.escape(str(q.get("type", "MCQ")))
                options_html = ""
                for oi, opt in enumerate(q.get("options", [])):
                    opt_safe = html_module.escape(str(opt))
                    options_html += (
                        '<button class="quiz-opt" data-qi="' + str(q_num - 1)
                        + '" data-oi="' + str(oi) + '">' + opt_safe + '</button>\n'
                    )
                expl = html_module.escape(str(q.get("explanation", "")))
                cards += (
                    '<div class="quiz-question" id="qq-' + str(q_num - 1) + '">'
                    + '<div class="qq-header"><span class="qq-num">Q' + str(q_num)
                    + '</span><span class="qq-type">' + q_type + '</span></div>'
                    + '<div class="qq-text">' + q_text + '</div>'
                    + '<div class="qq-options">' + options_html + '</div>'
                    + '<div class="qq-expl" id="qq-expl-' + str(q_num - 1) + '">' + expl + '</div>'
                    + '</div>\n'
                )
            sets_html += (
                '<div class="quiz-set"><div class="quiz-set-title">'
                + set_names[set_idx] + '</div>' + cards + '</div>\n'
            )

        answers_json = json.dumps([q.get("correct", 0) for q in questions[:15]])

        css_block = """
*, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
html, body { width:100%; min-height:100vh; background:#f8fafc;
  font-family:'Segoe UI',-apple-system,Arial,sans-serif; color:#1e293b;
  padding:24px 16px; overflow-y:auto; }
.quiz-container { max-width:720px; margin:0 auto; }
.quiz-header { text-align:center; margin-bottom:32px; }
.quiz-header h1 { font-size:24px; font-weight:700; color:#1e293b; margin-bottom:8px; }
.quiz-header p { font-size:14px; color:#64748b; }
.quiz-set { margin-bottom:28px; }
.quiz-set-title { font-size:16px; font-weight:700; color:#6366f1; margin-bottom:16px;
  padding:10px 18px; background:#eef2ff; border-radius:10px; border-left:4px solid #6366f1; }
.quiz-question { background:#ffffff; border:1px solid #e2e8f0; border-radius:12px;
  padding:20px 24px; margin-bottom:14px; box-shadow:0 1px 4px rgba(0,0,0,0.04);
  transition:border-color 0.2s; }
.quiz-question:hover { border-color:#c7d2fe; }
.qq-header { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.qq-num { font-size:12px; font-weight:700; color:#fff; background:#6366f1;
  padding:3px 10px; border-radius:50px; }
.qq-type { font-size:11px; font-weight:600; color:#6366f1; background:#eef2ff;
  padding:2px 8px; border-radius:50px; }
.qq-text { font-size:14px; line-height:1.7; color:#334155; margin-bottom:14px; font-weight:500; }
.qq-options { display:flex; flex-direction:column; gap:8px; }
.quiz-opt { display:block; width:100%; text-align:left; padding:10px 16px;
  border-radius:10px; border:1px solid #e2e8f0; background:#fafbfc;
  color:#475569; font-size:13px; font-family:inherit; cursor:pointer;
  transition:all 0.2s ease; }
.quiz-opt:hover:not([disabled]) { background:#eef2ff; border-color:#a5b4fc; color:#4338ca; }
.quiz-opt.correct { background:#ecfdf5; border-color:#10b981; color:#047857; font-weight:600; }
.quiz-opt.wrong { background:#fef2f2; border-color:#ef4444; color:#b91c1c; }
.qq-expl { display:none; font-size:12px; color:#64748b; margin-top:12px;
  padding:10px 14px; border-radius:8px; background:#f1f5f9; border:1px solid #e2e8f0; line-height:1.6; }
.qq-expl.show { display:block; }
.quiz-score { display:none; text-align:center; padding:32px; background:#fff;
  border-radius:16px; border:1px solid #e2e8f0; box-shadow:0 4px 16px rgba(0,0,0,0.06);
  margin-top:24px; }
.quiz-score.show { display:block; }
.quiz-score h2 { font-size:28px; color:#6366f1; margin-bottom:8px; }
.quiz-score p { font-size:14px; color:#64748b; margin-bottom:16px; }
.quiz-retry { padding:10px 28px; border-radius:50px; border:1px solid #c7d2fe;
  background:#eef2ff; color:#6366f1; font-size:13px; font-weight:600;
  cursor:pointer; font-family:inherit; transition:background 0.2s; }
.quiz-retry:hover { background:#c7d2fe; }
@media(max-width:600px) { .quiz-container { padding:0 4px; } }
"""

        js_block = """
(function() {
  var answers = """ + answers_json + """;
  var answered = {};
  var score = 0;
  var total = answers.length;
  document.addEventListener('click', function(e) {
    var opt = e.target.closest ? e.target.closest('.quiz-opt') : null;
    if (!opt) return;
    var qi = parseInt(opt.getAttribute('data-qi'), 10);
    var oi = parseInt(opt.getAttribute('data-oi'), 10);
    if (answered[qi] !== undefined) return;
    answered[qi] = oi;
    var correct = answers[qi];
    var opts = document.querySelectorAll('.quiz-opt[data-qi=\"' + qi + '\"]');
    for (var i = 0; i < opts.length; i++) {
      opts[i].setAttribute('disabled', '1');
      opts[i].style.pointerEvents = 'none';
    }
    opts[correct].classList.add('correct');
    if (oi !== correct) { opts[oi].classList.add('wrong'); }
    else { score++; }
    var expl = document.getElementById('qq-expl-' + qi);
    if (expl) expl.classList.add('show');
    if (Object.keys(answered).length >= total) {
      setTimeout(function() {
        var pct = Math.round(score / total * 100);
        var sv = document.getElementById('quiz-score');
        document.getElementById('score-val').textContent = pct + '%';
        var stars = pct >= 80 ? 'Excellent!' : pct >= 50 ? 'Good job!' : 'Keep practicing!';
        document.getElementById('score-msg').textContent = stars + ' ' + score + '/' + total + ' correct';
        sv.classList.add('show');
        sv.scrollIntoView({ behavior: 'smooth' });
      }, 600);
    }
  });
})();
"""

        return (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
            + '<meta charset="UTF-8">\n'
            + '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            + '<title>QAnim Quiz</title>\n'
            + '<style>' + css_block + '</style>\n'
            + '</head>\n<body>\n'
            + '<div class="quiz-container">\n'
            + '<div class="quiz-header"><h1>Quiz</h1>'
            + '<p>3 Sets &times; 5 Questions &mdash; ' + cat_safe + '</p></div>\n'
            + sets_html
            + '<div class="quiz-score" id="quiz-score">'
            + '<h2 id="score-val">0%</h2>'
            + '<p id="score-msg">Loading...</p>'
            + '<button class="quiz-retry" onclick="location.reload()">Retry Quiz</button>'
            + '</div>\n'
            + '</div>\n'
            + '<script>' + js_block + '</script>\n'
            + '</body>\n</html>'
        )


# ======================================================================
#  MODULE 7.65 — Answer Validator (NEW in v10.0)
#  Smart answer comparison with numeric tolerance.
# ======================================================================

class AnswerValidator:
    """
    Validates student answers against correct answers.
    Supports: exact match, numeric tolerance, string normalization.
    Returns: 'correct', 'nearly_correct', or 'wrong'.
    """

    @classmethod
    def validate(cls, user_answer: str, correct_answer: str,
                 tolerance: float = None) -> str:
        if tolerance is None:
            tolerance = ANSWER_TOLERANCE
        if not user_answer or not user_answer.strip():
            return "wrong"

        user_clean = cls._normalize(user_answer)
        correct_clean = cls._normalize(correct_answer)

        # Case 1: Exact string match
        if user_clean == correct_clean:
            return "correct"

        # Case 2: Numeric comparison
        user_num = cls._extract_number(user_answer)
        correct_num = cls._extract_number(correct_answer)
        if user_num is not None and correct_num is not None:
            if correct_num == 0:
                if abs(user_num) <= tolerance:
                    return "correct"
                elif abs(user_num) <= tolerance * 10:
                    return "nearly_correct"
                return "wrong"
            relative_diff = abs(user_num - correct_num) / abs(correct_num)
            if relative_diff <= tolerance:
                return "correct"
            elif relative_diff <= tolerance * 5:
                return "nearly_correct"
            return "wrong"

        # Case 3: Fuzzy containment
        if correct_clean in user_clean or user_clean in correct_clean:
            return "nearly_correct"

        return "wrong"

    @classmethod
    def _normalize(cls, text: str) -> str:
        t = text.strip().lower()
        t = re.sub(r'[.,;:!?\'"()\[\]{}]', '', t)
        t = re.sub(r'\s+', ' ', t).strip()
        return t

    @classmethod
    def _extract_number(cls, text: str) -> float:
        try:
            return float(text.strip())
        except (ValueError, TypeError):
            pass
        m = re.search(r'-?\d+\.?\d*(?:[eE][+-]?\d+)?', text)
        if m:
            try:
                return float(m.group(0))
            except (ValueError, TypeError):
                pass
        return None



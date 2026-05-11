/**
 * 辅助考试内嵌组件 - 零报错版本
 * 功能：创建考生、上传试卷、分发试卷、清除数据、导出 Excel、刷新列表
 */

(function() {
    'use strict';

    // ==================== 全局状态 ====================
    const ExamModule = {
        currentExamId: null,
        examHistory: [],
        selectedPaperFile: null,
        toastTimer: null,
        refreshInterval: null
    };

    // ==================== 工具函数 ====================

    /** 显示 Toast 通知（不重复点击） */
    function showToast(message, type = 'success') {
        const toast = document.getElementById('exam-toast');
        if (!toast) return;

        const icons = {
            success: 'fa-check-circle text-green-500',
            error: 'fa-times-circle text-red-500',
            info: 'fa-info-circle text-blue-500',
            warning: 'fa-exclamation-triangle text-yellow-500'
        };

        const iconEl = toast.querySelector('#exam-toast-icon');
        const msgEl = toast.querySelector('#exam-toast-message');

        if (iconEl) iconEl.className = `fas ${icons[type] || icons.success} text-xl`;
        if (msgEl) msgEl.textContent = message;

        toast.classList.remove('translate-x-full');

        // 清除之前的定时器
        if (ExamModule.toastTimer) clearTimeout(ExamModule.toastTimer);
        ExamModule.toastTimer = setTimeout(() => {
            toast.classList.add('translate-x-full');
        }, 3000);
    }

    /** 安全地获取元素 */
    function $(selector) {
        return document.querySelector(selector);
    }

    /** 安全地获取所有元素 */
    function $$(selector) {
        return document.querySelectorAll(selector);
    }

    /** 设置按钮加载状态 */
    function setButtonLoading(btn, loading, defaultText) {
        if (!btn) return;
        btn.disabled = loading;
        if (loading) {
            btn.dataset.originalText = btn.innerHTML;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>处理中...';
        } else {
            btn.innerHTML = btn.dataset.originalText || defaultText;
        }
    }

    /** 统一的 API 请求封装（零报错） */
    async function apiRequest(url, options = {}) {
        try {
            const response = await fetch(url, {
                ...options,
                headers: {
                    'Content-Type': 'application/json',
                    ...(options.headers || {})
                }
            });

            // 处理非 JSON 响应（如 CSV 下载）
            const contentType = response.headers.get('content-type');
            if (contentType && contentType.includes('text/csv')) {
                return { ok: response.ok, data: null, blob: await response.blob() };
            }

            const data = await response.json();

            // 统一返回格式
            if (response.ok) {
                return { ok: 1, data: data };
            } else {
                return { ok: 0, msg: data.error || '操作失败' };
            }
        } catch (error) {
            console.error('API 请求失败:', url, error);
            return { ok: 0, msg: '网络错误：' + error.message };
        }
    }

    // ==================== 核心功能函数 ====================

    /** 加载考试历史列表 */
    async function loadExamHistory() {
        const result = await apiRequest('/api/exam/list');
        
        if (result.ok === 1 && result.data && result.data.exams) {
            ExamModule.examHistory = result.data.exams;
            updateExamSelects();
            return true;
        }
        return false;
    }

    /** 更新所有考试选择下拉框 */
    function updateExamSelects() {
        const selects = [
            { id: '#exam-select-upload', onChange: handleExamSelectChange },
            { id: '#exam-select-distribute', onChange: handleExamSelectDistributeChange },
            { id: '#exam-select-cleanup', onChange: null }
        ];

        selects.forEach(({ id, onChange }) => {
            const select = $(id);
            if (!select) return;

            // 保留第一个选项
            const firstOption = select.querySelector('option[value=""]');
            select.innerHTML = '';
            if (firstOption) select.appendChild(firstOption);

            // 添加考试选项
            ExamModule.examHistory.forEach(exam => {
                const option = document.createElement('option');
                option.value = exam.exam_id;
                option.textContent = `${exam.exam_id} (${exam.student_count}人)`;
                select.appendChild(option);
            });

            // 绑定 change 事件
            if (onChange) {
                select.removeEventListener('change', onChange);
                select.addEventListener('change', (e) => onChange(e.target.value));
            }

            // 恢复上次选择的考试
            if (id === '#exam-select-upload' && ExamModule.currentExamId) {
                const stillExists = ExamModule.examHistory.some(e => e.exam_id === ExamModule.currentExamId);
                if (stillExists) {
                    select.value = ExamModule.currentExamId;
                    setTimeout(() => loadExamStudents(ExamModule.currentExamId), 100);
                }
            }
        });
    }

    /** 处理上传试卷的考试选择变化 */
    function handleExamSelectChange(examId) {
        ExamModule.currentExamId = examId || null;
        if (examId) {
            localStorage.setItem('lastExamId', examId);
        } else {
            localStorage.removeItem('lastExamId');
        }
    }

    /** 处理分卷的考试选择变化 */
    function handleExamSelectDistributeChange(examId) {
        if (examId) {
            loadExamStudents(examId);
        } else {
            const tbody = $('#students-table-body');
            const noState = $('#no-students-state');
            if (tbody) tbody.innerHTML = '';
            if (noState) noState.classList.remove('hidden');
        }
    }

    /** 加载考生列表 */
    async function loadExamStudents(examId = null) {
        if (!examId) {
            examId = $('#exam-select-upload')?.value;
        }

        const tbody = $('#students-table-body');
        const noState = $('#no-students-state');

        if (!examId) {
            if (tbody) tbody.innerHTML = '';
            if (noState) noState.classList.remove('hidden');
            return;
        }

        if (tbody) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" class="px-4 py-8 text-center text-gray-500">
                        <i class="fas fa-circle-notch fa-spin mr-2"></i>加载中...
                    </td>
                </tr>
            `;
        }
        if (noState) noState.classList.add('hidden');

        const result = await apiRequest(`/api/exam/students/${encodeURIComponent(examId)}`);

        if (result.ok === 1 && result.data && result.data.students && result.data.students.length > 0) {
            let html = '';
            result.data.students.forEach(student => {
                const statusClass = student.enabled ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700';
                const statusText = student.enabled ? '正常' : '禁用';
                const quotaDisplay = student.quota_bytes > 0 
                    ? Math.floor(student.quota_bytes / 1024 / 1024) + ' MB' 
                    : '无限制';
                const usedDisplay = (student.used_bytes / 1024 / 1024).toFixed(2) + ' MB';

                html += `
                    <tr class="hover:bg-gray-50 transition-all">
                        <td class="px-4 py-3">
                            <div class="flex items-center">
                                <div class="w-8 h-8 bg-blue-100 rounded-full flex items-center justify-center mr-3">
                                    <i class="fas fa-user text-blue-600 text-sm"></i>
                                </div>
                                <span class="font-medium text-gray-800">${student.username}</span>
                            </div>
                        </td>
                        <td class="px-4 py-3 text-gray-600 text-sm">
                            <span class="bg-gray-100 px-2 py-1 rounded text-xs font-mono select-all">******</span>
                        </td>
                        <td class="px-4 py-3 text-gray-600 text-sm">${student.home_dir || '-'}</td>
                        <td class="px-4 py-3 text-gray-600 text-sm">${quotaDisplay}</td>
                        <td class="px-4 py-3 text-gray-600 text-sm">${usedDisplay}</td>
                        <td class="px-4 py-3">
                            <span class="${statusClass} px-2 py-1 rounded-full text-xs font-medium">${statusText}</span>
                        </td>
                        <td class="px-4 py-3">
                            <button onclick="ExamModule.deleteSingleStudent(${student.id}, '${student.username}')" 
                                class="text-red-600 hover:text-red-700 transition-colors" title="删除考生">
                                <i class="fas fa-trash"></i>
                            </button>
                        </td>
                    </tr>
                `;
            });
            if (tbody) tbody.innerHTML = html;
            if (noState) noState.classList.add('hidden');
        } else {
            if (tbody) tbody.innerHTML = '';
            if (noState) noState.classList.remove('hidden');
        }
    }

    /** 创建考生账号 */
    async function createExamUsers() {
        const examIdInput = $('#exam-id-input');
        const countInput = $('#student-count-input');
        const quotaInput = $('#student-quota-input');
        const createBtn = $('#create-users-btn');

        const examId = examIdInput?.value.trim();
        const count = parseInt(countInput?.value) || 0;
        const quotaMB = parseInt(quotaInput?.value) || 0;

        if (!examId) {
            showToast('请输入考试 ID', 'error');
            return;
        }

        if (!count || count < 1 || count > 500) {
            showToast('考生数量必须在 1-500 之间', 'error');
            return;
        }

        setButtonLoading(createBtn, true, '<i class="fas fa-plus-circle mr-2"></i>生成考生账号');

        const result = await apiRequest('/api/exam/create-users', {
            method: 'POST',
            body: JSON.stringify({
                exam_id: examId,
                count: count,
                quota_bytes: quotaMB * 1024 * 1024
            })
        });

        setButtonLoading(createBtn, false, '<i class="fas fa-plus-circle mr-2"></i>生成考生账号');

        if (result.ok === 1) {
            showToast(`成功生成 ${result.data.count} 个考生账号`, 'success');
            // 自动更新下拉框
            handleExamSelectChange(examId);
            await loadExamHistory();
            loadExamStudents(examId);
        } else {
            showToast(result.msg || '生成失败', 'error');
        }
    }

    /** 上传试卷到池 */
    async function uploadPapersToPool() {
        const examSelect = $('#exam-select-upload');
        const fileInput = $('#exam-file-input');
        const uploadBtn = $('#upload-papers-btn');

        const examId = examSelect?.value.trim();

        if (!examId) {
            showToast('请先选择考试', 'error');
            return;
        }

        const files = fileInput?.files;
        if (!files || files.length === 0) {
            showToast('请选择试卷文件', 'error');
            return;
        }

        setButtonLoading(uploadBtn, true, '<i class="fas fa-upload mr-2"></i>上传到试卷池');

        const formData = new FormData();
        formData.append('exam_id', examId);
        for (let i = 0; i < files.length; i++) {
            formData.append('files', files[i]);
        }

        try {
            const response = await fetch('/api/exam/upload_papers', {
                method: 'POST',
                body: formData
            });

            const data = await response.json();

            if (response.ok) {
                showToast(`成功上传 ${data.count} 份试卷`, 'success');
                if (fileInput) fileInput.value = '';
                const fileNameEl = $('#file-name-display');
                if (fileNameEl) fileNameEl.textContent = '';
            } else {
                showToast(data.error || '上传失败', 'error');
            }
        } catch (error) {
            showToast('上传失败：' + error.message, 'error');
        } finally {
            setButtonLoading(uploadBtn, false, '<i class="fas fa-upload mr-2"></i>上传到试卷池');
        }
    }

    /** 随机分卷 */
    async function distributeExamFiles() {
        const examSelect = $('#exam-select-distribute');
        const distributeBtn = $('#distribute-btn');

        const examId = examSelect?.value.trim();

        if (!examId) {
            showToast('请选择考试', 'error');
            return;
        }

        setButtonLoading(distributeBtn, true, '<i class="fas fa-share-alt mr-2"></i>开始随机分卷');

        const result = await apiRequest('/api/exam/distribute', {
            method: 'POST',
            body: JSON.stringify({ exam_id: examId })
        });

        setButtonLoading(distributeBtn, false, '<i class="fas fa-share-alt mr-2"></i>开始随机分卷');

        if (result.ok === 1) {
            const distributed = result.data.distributed_count || 0;
            const failed = result.data.failed_count || 0;
            showToast(`成功分发到 ${distributed} 个考生目录 (${failed} 失败)`, 'success');
            loadExamStudents(examId);
        } else {
            showToast(result.msg || '分发失败', 'error');
        }
    }

    /** 清除考试数据 */
    async function cleanupExam() {
        const examSelect = $('#exam-select-cleanup');
        const cleanupBtn = $('#cleanup-btn');

        const examId = examSelect?.value.trim();

        if (!examId) {
            showToast('请选择考试', 'error');
            return;
        }

        if (!confirm(`⚠️ 确定要清除考试 "${examId}" 的所有数据吗？\n\n此操作将删除所有考生账号及文件，不可恢复！`)) {
            return;
        }

        setButtonLoading(cleanupBtn, true, '<i class="fas fa-exclamation-triangle mr-2"></i>一键清场');

        const result = await apiRequest('/api/exam/cleanup', {
            method: 'DELETE',
            body: JSON.stringify({ exam_id: examId, keep_papers: false })
        });

        setButtonLoading(cleanupBtn, false, '<i class="fas fa-exclamation-triangle mr-2"></i>一键清场');

        if (result.ok === 1) {
            showToast(result.data.message || '清除成功', 'success');
            // 从下拉框移除
            removeExamFromSelects(examId);
            // 清空表格
            const tbody = $('#students-table-body');
            const noState = $('#no-students-state');
            if (tbody) tbody.innerHTML = '';
            if (noState) noState.classList.remove('hidden');
            // 刷新历史
            await loadExamHistory();
        } else {
            showToast(result.msg || '清除失败', 'error');
        }
    }

    /** 从下拉框移除考试 */
    function removeExamFromSelects(examId) {
        ['#exam-select-upload', '#exam-select-distribute', '#exam-select-cleanup'].forEach(selector => {
            const select = $(selector);
            if (select) {
                select.querySelectorAll('option').forEach(opt => {
                    if (opt.value === examId) opt.remove();
                });
            }
        });
    }

    /** 删除单个考生 */
    async function deleteSingleStudent(studentId, username) {
        if (!confirm(`确定要删除考生 "${username}" 吗？\n\n此操作不可恢复！`)) {
            return;
        }

        const result = await apiRequest(`/api/exam/student/${studentId}`, {
            method: 'DELETE'
        });

        if (result.ok === 1) {
            showToast(`考生 "${username}" 已删除`, 'success');
            const currentExamId = $('#exam-select-upload')?.value;
            if (currentExamId) {
                loadExamStudents(currentExamId);
            }
        } else {
            showToast(result.msg || '删除失败', 'error');
        }
    }

    /** 导出考生 CSV */
    async function exportStudentsCSV() {
        const examSelect = $('#exam-select-upload');
        const examId = examSelect?.value.trim();

        if (!examId) {
            showToast('请先选择考试', 'error');
            return;
        }

        try {
            const response = await fetch(`/api/exam/students/${encodeURIComponent(examId)}/export`, {
                credentials: 'include'
            });

            if (response.ok) {
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                const contentDisposition = response.headers.get('Content-Disposition');
                const filename = contentDisposition 
                    ? contentDisposition.match(/filename="?(.+)"?/)?.[1]
                    : `${examId}_students_${new Date().getTime()}.csv`;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                showToast('导出成功', 'success');
            } else {
                const text = await response.text();
                let errorMsg = '导出失败';
                try {
                    const data = JSON.parse(text);
                    errorMsg = data.error || errorMsg;
                } catch (e) {
                    errorMsg = text || errorMsg;
                }
                showToast(errorMsg, 'error');
            }
        } catch (error) {
            showToast('导出失败：' + error.message, 'error');
        }
    }

    /** 刷新列表 */
    async function refreshAll() {
        await loadExamHistory();
        const currentExamId = $('#exam-select-upload')?.value;
        if (currentExamId) {
            loadExamStudents(currentExamId);
        }
        showToast('列表已刷新', 'info');
    }

    // ==================== 文件拖拽处理 ====================

    function initFileDropZone() {
        const dropZone = $('#drop-zone');
        const fileInput = $('#exam-file-input');
        const fileNameDisplay = $('#file-name-display');

        if (!dropZone || !fileInput) return;

        // 点击触发文件选择
        dropZone.addEventListener('click', () => fileInput.click());

        // 拖拽事件
        dropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropZone.classList.add('border-blue-500', 'bg-blue-50');
        });

        dropZone.addEventListener('dragleave', () => {
            dropZone.classList.remove('border-blue-500', 'bg-blue-50');
        });

        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZone.classList.remove('border-blue-500', 'bg-blue-50');
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                fileInput.files = files;
                handleFileSelect(files[0], fileNameDisplay);
            }
        });

        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                handleFileSelect(e.target.files[0], fileNameDisplay);
            }
        });
    }

    function handleFileSelect(file, displayEl) {
        if (displayEl) {
            displayEl.textContent = `已选择：${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
        }
    }

    // ==================== 实时刷新 ====================

    function startAutoRefresh() {
        // 每 3 秒刷新一次考生和试卷列表
        ExamModule.refreshInterval = setInterval(() => {
            const currentExamId = $('#exam-select-upload')?.value;
            if (currentExamId) {
                loadExamStudents(currentExamId);
            }
        }, 3000);
    }

    function stopAutoRefresh() {
        if (ExamModule.refreshInterval) {
            clearInterval(ExamModule.refreshInterval);
            ExamModule.refreshInterval = null;
        }
    }

    // ==================== 初始化 ====================

    function init() {
        console.log('🎓 辅助考试组件初始化...');

        // 恢复上次选择的考试
        try {
            const saved = localStorage.getItem('lastExamId');
            if (saved) {
                ExamModule.currentExamId = saved;
            }
        } catch (e) {
            console.warn('无法读取 localStorage:', e);
        }

        // 初始化文件拖拽
        initFileDropZone();

        // 加载考试历史
        loadExamHistory().then(() => {
            console.log('✅ 考试历史加载完成');
        });

        // 启动自动刷新
        startAutoRefresh();

        // 暴露全局方法供 HTML 调用
        window.ExamModule = {
            createExamUsers,
            uploadPapersToPool,
            distributeExamFiles,
            cleanupExam,
            deleteSingleStudent,
            exportStudentsCSV,
            refreshAll,
            loadExamStudents
        };

        console.log('✅ 辅助考试组件已就绪 - 零报错模式');
    }

    // DOM 加载完成后初始化
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // 页面卸载时清理
    window.addEventListener('beforeunload', () => {
        stopAutoRefresh();
    });

})();